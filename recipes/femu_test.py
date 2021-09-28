# Copyright 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from contextlib import contextmanager

from PB.recipes.flutter.engine import InputProperties
from PB.recipes.flutter.engine import EnvProperties

from PB.go.chromium.org.luci.buildbucket.proto import build as build_pb2
from google.protobuf import struct_pb2

import re

PYTHON_VERSION_COMPATIBILITY = 'PY2'

DEPS = [
    'depot_tools/depot_tools',
    'flutter/repo_util',
    'flutter/retry',
    'flutter/test_utils',
    'flutter/yaml',
    'fuchsia/archive',
    'fuchsia/display_util',
    'fuchsia/goma',
    'fuchsia/sdk',
    'fuchsia/ssh',
    'fuchsia/vdl',
    'recipe_engine/buildbucket',
    'recipe_engine/cas',
    'recipe_engine/context',
    'recipe_engine/file',
    'recipe_engine/json',
    'recipe_engine/path',
    'recipe_engine/platform',
    'recipe_engine/properties',
    'recipe_engine/raw_io',
    'recipe_engine/step',
    'recipe_engine/swarming',
]

PROPERTIES = InputProperties
ENV_PROPERTIES = EnvProperties


def GetCheckoutPath(api):
  return api.path['cache'].join('builder', 'src')


def Build(api, config, *targets):
  checkout = GetCheckoutPath(api)
  build_dir = checkout.join('out/%s' % config)
  goma_jobs = api.properties['goma_jobs']
  ninja_args = [api.depot_tools.ninja_path, '-j', goma_jobs, '-C', build_dir]
  ninja_args.extend(targets)
  with api.goma.build_with_goma():
    name = 'build %s' % ' '.join([config] + list(targets))
    api.step(name, ninja_args)


def GetFlutterFuchsiaBuildTargets(product, include_test_targets=False):
  targets = ['flutter/shell/platform/fuchsia:fuchsia']
  if include_test_targets:
    targets += ['fuchsia_tests']
  return targets


def BuildAndTestFuchsia(api, build_script, git_rev):
  RunGN(api, '--fuchsia', '--fuchsia-cpu', 'x64', '--runtime-mode', 'debug',
        '--no-lto')
  Build(api, 'fuchsia_debug_x64', *GetFlutterFuchsiaBuildTargets(False, True))
  fuchsia_package_cmd = [
      'python', build_script, '--engine-version', git_rev, '--skip-build',
      '--archs', 'x64', '--runtime-mode', 'debug'
  ]
  api.step('Package Fuchsia Artifacts', fuchsia_package_cmd)
  TestFuchsiaFEMU(api)


def RunGN(api, *args):
  checkout = GetCheckoutPath(api)
  gn_cmd = ['python', checkout.join('flutter/tools/gn'), '--goma']
  gn_cmd.extend(args)
  api.step('gn %s' % ' '.join(args), gn_cmd)


def GetFuchsiaBuildId(api):
  checkout = GetCheckoutPath(api)
  manifest_path = checkout.join('fuchsia', 'sdk', 'linux', 'meta',
                                'manifest.json')
  manifest_data = api.file.read_json('Read manifest', manifest_path)
  return manifest_data['id']


# TODO(yuanzhi) Move this logic to vdl recipe_module
def CasRoot(api):
  """Create a CAS containing flutter test components and fuchsia runfiles for FEMU."""
  sdk_version = GetFuchsiaBuildId(api)
  checkout = GetCheckoutPath(api)
  root_dir = api.path.mkdtemp('vdl_runfiles_')
  cas_tree = api.archive.tree(root=root_dir)
  test_suites = []

  def add(src, name_rel_to_root):
    # CAS requires the files to be located directly inside the root folder.
    cas_tree.register_link(
        target=src,
        linkname=cas_tree.root.join(name_rel_to_root),
    )

  def addVDLFiles():
    vdl_version = api.properties.get('vdl_version', 'g3-revision:vdl_fuchsia_20210813_RC00')
    api.vdl.set_vdl_cipd_tag(tag=str(vdl_version))
    add(api.vdl.vdl_path, 'device_launcher')
    add(api.vdl.aemu_dir, 'aemu')
    add(api.vdl.create_device_proto(), 'virtual_device.textproto')

  def addPackageFiles():
    fuchsia_packages = api.vdl.get_package_paths(sdk_version=sdk_version)
    add(fuchsia_packages.pm, api.path.basename(fuchsia_packages.pm))
    add(fuchsia_packages.amber_files,
        api.path.basename(fuchsia_packages.amber_files))

  def addImageFiles():
    ssh_files = api.vdl.gen_ssh_files()
    add(ssh_files.id_public, api.path.basename(ssh_files.id_public))
    add(ssh_files.id_private, api.path.basename(ssh_files.id_private))

    fuchsia_images = api.vdl.get_image_paths(sdk_version=sdk_version)
    add(fuchsia_images.build_args, "qemu_buildargs")
    add(fuchsia_images.kernel_file, "qemu_kernel")
    add(fuchsia_images.system_fvm, "qemu_fvm")
    add(api.sdk.sdk_path.join("tools", "far"), "far")
    add(api.sdk.sdk_path.join("tools", "fvm"), "fvm")

    ## Provision and add zircon-a
    authorized_zircona = api.buildbucket.builder_cache_path.join(
        'zircon-authorized.zbi')
    api.sdk.authorize_zbi(
        ssh_key_path=ssh_files.id_public,
        zbi_input_path=fuchsia_images.zircona,
        zbi_output_path=authorized_zircona,
    )
    add(authorized_zircona, "qemu_zircona-ed25519")

    ## Generate and add ssh_config
    ssh_config = api.buildbucket.builder_cache_path.join('ssh_config')
    api.ssh.generate_ssh_config(
        private_key_path=api.path.basename(ssh_files.id_private),
        dest=ssh_config)
    add(ssh_config, "ssh_config")

  def addFlutterTests():
    add(
        checkout.join('out', 'fuchsia_bucket', 'flutter', 'x64', 'debug', 'aot',
                      'flutter_aot_runner-0.far'), 'flutter_aot_runner-0.far')
    test_suites_file = checkout.join(
      'flutter', 'testing', 'fuchsia', 'test_suites.yaml')
    for suite in api.yaml.read('Retrieve list of test suites',
                      test_suites_file, api.json.output()).json.output:
      # Ensure command is well-formed.
      # See https://fuchsia.dev/fuchsia-src/concepts/packages/package_url.
      match = re.match(r'^(run-test-component|run-test-suite) fuchsia-pkg://[0-9a-z\-_\.]+/(?P<name>[0-9a-z\-_\.]+)#meta/[0-9a-z\-_\.]+(\.cm|\.cmx)( +[0-9a-zA-Z\-_*\.: =]+)?$', suite['test_command'])
      if not match:
        raise api.step.StepFailure('Invalid test command: %s' % suite['test_command'])
      suite['name'] = match.group('name')
      if 'packages' not in suite:
        suite['packages'] = [ suite['package'] ]
      suite['package_basenames'] = []
      for path in suite['packages']:
        basename = re.match(r'(:?.*/)*([^/]*$)', path).group(2)
        suite['package_basenames'].append(basename)
        add(checkout.join('out', 'fuchsia_debug_x64', path), basename)
      test_suites.append(suite)

  def addTestScript():
    test_script = api.resource('run_vdl_test.sh')
    api.step('change file permission', ['chmod', '777', test_script])
    add(test_script, "run_vdl_test.sh")

  addVDLFiles()
  addPackageFiles()
  addImageFiles()
  addTestScript()
  addFlutterTests()

  cas_tree.create_links("create tree of vdl runfiles")
  cas_hash = api.archive.upload(cas_tree.root, step_name='Archive FEMU Run Files')
  return test_suites, root_dir, cas_hash


def TestFuchsiaFEMU(api):
  """Run flutter tests on FEMU."""
  test_suites, root_dir, cas_hash = CasRoot(api)
  cmd = ['./run_vdl_test.sh']
  # These flags will be passed through to VDL
  cmd.append('--emulator_binary_path=aemu/emulator')
  cmd.append('--proto_file_path=virtual_device.textproto')
  cmd.append('--pm_tool=pm')
  cmd.append('--far_tool=far')
  cmd.append('--fvm_tool=fvm')
  cmd.append('--resize_fvm=2G')
  cmd.append('--gpu=swiftshader_indirect')
  cmd.append('--headless_mode=true')
  cmd.append(
      '--system_images=' \
      '{build_args},{kernel},{fvm},{zircona},{ssh_config},{ssh_id_public},' \
      '{ssh_id_private},{amber_files}' \
      .format(
          build_args='qemu_buildargs',
          kernel='qemu_kernel',
          fvm='qemu_fvm',
          zircona='qemu_zircona-ed25519',
          ssh_config='ssh_config',
          ssh_id_public='id_ed25519.pub',
          ssh_id_private='id_ed25519',
          amber_files='amber-files',
      ))

  with api.context(cwd=root_dir):
    with api.step.nest('FEMU Test'), api.step.defer_results():
      for suite in test_suites:
        test_cmd = cmd + [
          '--serve_packages=flutter_aot_runner-0.far,%s' % ','.join(suite['package_basenames']),
          '--test_suite=%s' % suite['name'],
          '--test_command=%s' % suite['test_command'],
          '--emulator_log',
          api.raw_io.output_text(name='emulator_log'),
          '--syslog',
          api.raw_io.output_text(name='syslog')
        ]
        api.retry.step(
            api.test_utils.test_step_name(
                'Run FEMU Test Suite %s' % suite['name']
            ),
            test_cmd,
            step_test_data=(
                lambda: api.raw_io.test_api.
                output_text('failure', name='syslog')
            )
        )

def BuildFuchsia(api):
  """
  Schedules release builds for x64 on other bots, and then builds the x64 runners
  (which do not require LTO and thus are faster to build).

  On Linux, we also run tests for the runner against x64, and if they fail
  we cancel the scheduled builds.
  """

  checkout = GetCheckoutPath(api)
  build_script = str(
      checkout.join('flutter/tools/fuchsia/build_fuchsia_artifacts.py'))
  git_rev = api.buildbucket.gitiles_commit.id or 'HEAD'
  BuildAndTestFuchsia(api, build_script, git_rev)


def RunSteps(api, properties, env_properties):
  cache_root = api.buildbucket.builder_cache_path
  checkout = GetCheckoutPath(api)
  api.file.rmtree('Clobber build output', checkout.join('out'))
  api.file.ensure_directory('Ensure checkout cache', cache_root)
  api.goma.ensure()
  dart_bin = checkout.join('third_party', 'dart', 'tools', 'sdks', 'dart-sdk',
                           'bin')

  env = {'GOMA_DIR': api.goma.goma_dir}
  env_prefixes = {'PATH': [dart_bin]}

  api.repo_util.engine_checkout(
      cache_root, env, env_prefixes, clobber=properties.clobber)

  # Various scripts we run assume access to depot_tools on path for `ninja`.
  with api.context(
      cwd=cache_root, env=env,
      env_prefixes=env_prefixes), api.depot_tools.on_path():
    if api.platform.is_linux and api.properties.get('build_fuchsia', True):
      BuildFuchsia(api)


############ RECIPE TEST ############


def GenTests(api):
  output_props = struct_pb2.Struct()
  output_props['cas_output_hash'] = 'deadbeef'
  build = api.buildbucket.try_build_message(
      builder='FEMU Test', project='flutter')
  build.output.CopyFrom(build_pb2.Build.Output(properties=output_props))

  yield api.test(
      'start_femu_with_vdl',
      api.properties(
          InputProperties(
              goma_jobs='1024',
              build_fuchsia=True,
              test_fuchsia=True,
              git_url='https://github.com/flutter/engine',
              git_ref='refs/pull/1/head',
              clobber=False,
          ),),
      api.step_data(
          'Retrieve list of test suites.parse',
          api.json.output([{
            'package': 'v2_test-123.far',
            'test_command': 'run-test-suite fuchsia-pkg://fuchsia.com/v2_test#meta/v2_test.cm -- --gtest_filter=-ParagraphTest.*:a.b'
          }, {
            'package': 'v1_test_component-321.far',
            'test_command': 'run-test-component fuchsia-pkg://fuchsia.com/v1_test_component#meta/v1_test_component.cmx -ParagraphTest.*:a.b'
          }])
      ),
      api.step_data(
          'Retrieve list of test suites.read',
          api.file.read_text('''# This is a comment.
- package: v2_test-123.far
  test_command: run-test-suite fuchsia-pkg://fuchsia.com/v2_test#meta/v2_test.cm -- --gtest_filter=-ParagraphTest.*:a.b

# Legacy cfv1 test
- package: v1_test_component-321.far
  test_command: run-test-component fuchsia-pkg://fuchsia.com/v1_test_component#meta/v1_test_component.cmx -ParagraphTest.*:a.b''')
      ),
      api.step_data(
          'Read manifest',
          api.file.read_json({'id': '0.20200101.0.1'}),
      ),
      api.step_data('FEMU Test.test: Run FEMU Test Suite v2_test', retcode=1),
      api.step_data('FEMU Test.test: Run FEMU Test Suite v1_test_component', retcode=1),
      api.properties.environ(EnvProperties(SWARMING_TASK_ID='deadbeef')),
      api.platform('linux', 64),
      api.path.exists(
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/buildargs.gn'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/qemu-kernel.kernel'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/storage-full.blk'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/zircon-a.zbi'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/pm'),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/amber-files'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/qemu-x64.tar.gz'
          ),
          api.path['cache'].join('builder/ssh/id_ed25519.pub'),
          api.path['cache'].join('builder/ssh/id_ed25519'),
          api.path['cache'].join('builder/ssh/ssh_host_key.pub'),
          api.path['cache'].join('builder/ssh/ssh_host_key'),
      ),
  )

  yield api.test(
      'femu_vdl_with_package_list',
      api.properties(
          InputProperties(
              goma_jobs='1024',
              build_fuchsia=True,
              test_fuchsia=True,
              git_url='https://github.com/flutter/engine',
              git_ref='refs/pull/1/head',
              clobber=False,
          ),),
      api.step_data(
          'Retrieve list of test suites.parse',
          api.json.output([{
            'test_command': 'run-test-suite fuchsia-pkg://fuchsia.com/v2_test#meta/v2_test.cm -- --gtest_filter=-ParagraphTest.*:a.b',
            'packages': [
              'v2_test-123.far'
            ]
          }, {
            'test_command': 'run-test-component fuchsia-pkg://fuchsia.com/v1_test_component#meta/v1_test_component.cmx -ParagraphTest.*:a.b',
            'packages': [
              'v1_test_component-321.far'
            ]
          }])
      ),
      api.step_data(
          'Retrieve list of test suites.read',
          api.file.read_text('''# This is a comment.
- test_command: run-test-suite fuchsia-pkg://fuchsia.com/v2_test#meta/v2_test.cm -- --gtest_filter=-ParagraphTest.*:a.b
  packages:
    - v2_test-123.far

# Legacy cfv1 test
- test_command: run-test-component fuchsia-pkg://fuchsia.com/v1_test_component#meta/v1_test_component.cmx -ParagraphTest.*:a.b
  packages:
    - v1_test_component-321.far''')
      ),
      api.step_data(
          'Read manifest',
          api.file.read_json({'id': '0.20200101.0.1'}),
      ),
      api.step_data('FEMU Test.test: Run FEMU Test Suite v2_test', retcode=1),
      api.step_data('FEMU Test.test: Run FEMU Test Suite v1_test_component', retcode=1),
      api.properties.environ(EnvProperties(SWARMING_TASK_ID='deadbeef')),
      api.platform('linux', 64),
      api.path.exists(
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/buildargs.gn'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/qemu-kernel.kernel'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/storage-full.blk'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/zircon-a.zbi'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/pm'),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/amber-files'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/qemu-x64.tar.gz'
          ),
          api.path['cache'].join('builder/ssh/id_ed25519.pub'),
          api.path['cache'].join('builder/ssh/id_ed25519'),
          api.path['cache'].join('builder/ssh/ssh_host_key.pub'),
          api.path['cache'].join('builder/ssh/ssh_host_key'),
      ),
  )

  yield api.test(
      'multiple_non_root_fars',
      api.properties(
          InputProperties(
              goma_jobs='1024',
              build_fuchsia=True,
              test_fuchsia=True,
              git_url='https://github.com/flutter/engine',
              git_ref='refs/pull/1/head',
              clobber=False,
          ),),
      api.step_data(
          'Retrieve list of test suites.parse',
          api.json.output([{
            'test_command': 'run-test-component fuchsia-pkg://fuchsia.com/flutter-embedder-test#meta/flutter-embedder-test.cmx',
            'packages': [
              'flutter-embedder-test-0.far',
              'gen/flutter/shell/platform/fuchsia/flutter/integration_flutter_tests/embedder/child-view/child-view/child-view.far',
              'gen/flutter/shell/platform/fuchsia/flutter/integration_flutter_tests/embedder/parent-view/parent-view/parent-view.far'
            ]
          }])
      ),
      api.step_data(
          'Retrieve list of test suites.read',
          api.file.read_text('''# This is a comment.
- test_command: run-test-component fuchsia-pkg://fuchsia.com/flutter-embedder-test#meta/flutter-embedder-test.cmx
  packages:
    - flutter-embedder-test-0.far
    - gen/flutter/shell/platform/fuchsia/flutter/integration_flutter_tests/embedder/child-view/child-view/child-view.far
    - gen/flutter/shell/platform/fuchsia/flutter/integration_flutter_tests/embedder/parent-view/parent-view/parent-view.far''')
      ),
      api.step_data(
          'Read manifest',
          api.file.read_json({'id': '0.20200101.0.1'}),
      ),
      api.step_data('FEMU Test.test: Run FEMU Test Suite flutter-embedder-test', retcode=1),
      api.properties.environ(EnvProperties(SWARMING_TASK_ID='deadbeef')),
      api.platform('linux', 64),
      api.path.exists(
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/buildargs.gn'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/qemu-kernel.kernel'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/storage-full.blk'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/zircon-a.zbi'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/pm'),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/amber-files'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/qemu-x64.tar.gz'
          ),
          api.path['cache'].join('builder/ssh/id_ed25519.pub'),
          api.path['cache'].join('builder/ssh/id_ed25519'),
          api.path['cache'].join('builder/ssh/ssh_host_key.pub'),
          api.path['cache'].join('builder/ssh/ssh_host_key'),
      ),
  )

  yield api.test(
      'no_zircon_file',
      api.properties(
          InputProperties(
              goma_jobs='1024',
              build_fuchsia=True,
              test_fuchsia=True,
              git_url='https://github.com/flutter/engine',
              git_ref='refs/pull/1/head',
              vdl_version='g3-revision:vdl_fuchsia_xxxxxxxx_RC00',
          ),),
      api.step_data(
          'Read manifest',
          api.file.read_json({'id': '0.20200101.0.1'}),
      ),
      api.platform('linux', 64),
      api.path.exists(
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/buildargs.gn'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/qemu-kernel.kernel'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/storage-full.blk'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/pm'),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/amber-files'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/qemu-x64.tar.gz'
          ),
          api.path['cache'].join('builder/ssh/id_ed25519.pub'),
          api.path['cache'].join('builder/ssh/id_ed25519'),
          api.path['cache'].join('builder/ssh/ssh_host_key.pub'),
          api.path['cache'].join('builder/ssh/ssh_host_key'),
      ),
  )

  yield api.test(
      'dangerous_test_commands',
      api.properties(
          InputProperties(
              goma_jobs='1024',
              build_fuchsia=True,
              test_fuchsia=True,
              git_url='https://github.com/flutter/engine',
              git_ref='refs/pull/1/head',
              clobber=False,
          ),),
      api.step_data(
          'Retrieve list of test suites.parse',
          api.json.output([{
            'package': 'ordinary_package1.far',
            'test_command': 'suspicious command'
          }, {
            'package': 'ordinary_package2.far',
            'test_command': 'run-test-suite fuchsia-pkg://fuchsia.com/ordinary_package2#meta/ordinary_package2.cmx; suspicious command'
          }, {
            'package': 'ordinary_package3.far',
            'test_command': 'run-test-suite fuchsia-pkg://fuchsia.com/ordinary_package3#meta/ordinary_package3.cmx $(suspicious command)'
          }, {
            'package': 'ordinary_package4.far',
            'test_command': 'run-test-suite fuchsia-pkg://fuchsia.com/ordinary_package4#meta/ordinary_package4.cmx `suspicious command`'
          }])
      ),
      api.step_data(
          'Retrieve list of test suites.read',
          api.file.read_text('''
- package: ordinary_package1.far
  test_command: suspicious command
- package: ordinary_package2.far
  test_command: run-test-suite fuchsia-pkg://fuchsia.com/ordinary_package2#meta/ordinary_package2.cmx; suspicious command
- package: ordinary_package3.far
  test_command: run-test-suite fuchsia-pkg://fuchsia.com/ordinary_package3#meta/ordinary_package3.cmx $(suspicious command)
- package: ordinary_package4.far
  test_command: run-test-suite fuchsia-pkg://fuchsia.com/ordinary_package4#meta/ordinary_package4.cmx `suspicious command`''')
      ),
      api.step_data(
          'Read manifest',
          api.file.read_json({'id': '0.20200101.0.1'}),
      ),
      api.properties.environ(EnvProperties(SWARMING_TASK_ID='deadbeef')),
      api.platform('linux', 64),
      api.path.exists(
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/buildargs.gn'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/qemu-kernel.kernel'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/storage-full.blk'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_image/linux_intel_64/zircon-a.zbi'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/pm'),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/amber-files'
          ),
          api.path['cache'].join(
              'builder/0.20200101.0.1/fuchsia_packages/linux_intel_64/qemu-x64.tar.gz'
          ),
          api.path['cache'].join('builder/ssh/id_ed25519.pub'),
          api.path['cache'].join('builder/ssh/id_ed25519'),
          api.path['cache'].join('builder/ssh/ssh_host_key.pub'),
          api.path['cache'].join('builder/ssh/ssh_host_key'),
      ),
  )
