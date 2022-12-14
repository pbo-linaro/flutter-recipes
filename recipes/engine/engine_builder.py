# Copyright 2016 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from contextlib import contextmanager
import contextlib

from PB.recipes.flutter.engine.engine_builder import InputProperties, EngineBuild

DEPS = [
  'depot_tools/depot_tools',
  'flutter/os_utils',
  'flutter/osx_sdk',
  'flutter/repo_util',
  'flutter/shard_util_v2',
  'fuchsia/goma',
  'recipe_engine/buildbucket',
  'recipe_engine/cas',
  'recipe_engine/context',
  'recipe_engine/path',
  'recipe_engine/platform',
  'recipe_engine/properties',
  'recipe_engine/step',
]

GIT_REPO = \
    'https://flutter.googlesource.com/mirrors/engine'

PROPERTIES = InputProperties


def Build(api, config, disable_goma, *targets):
  checkout = api.path['cache'].join('builder', 'src')
  build_dir = checkout.join('out/%s' % config)

  if not disable_goma:
    ninja_args = [api.depot_tools.autoninja_path, '-C', build_dir]
    ninja_args.extend(targets)
    with api.goma.build_with_goma():
      name='build %s' % ' '.join([config] + list(targets))
      api.step(name, ninja_args)
  else:
    ninja_args = [api.depot_tools.autoninja_path, '-C', build_dir]
    ninja_args.extend(targets)
    api.step('build %s' % ' '.join([config] + list(targets)), ninja_args)


def RunGN(api, *args):
  checkout = api.path['cache'].join('builder', 'src')
  gn_cmd = ['python', checkout.join('flutter/tools/gn')]
  gn_cmd.extend(args)
  api.step('gn %s' % ' '.join(args), gn_cmd)


def CasOutputs(api, output_files, output_dirs):
  out_dir = api.path['cache'].join('builder', 'src')
  dirs = output_files + output_dirs
  dirs = [api.path.abspath(d) for d in dirs]
  return api.cas.archive('CAS build outputs', out_dir, *dirs)


def RunSteps(api, properties):
  # Collect memory/cpu/process after task execution.
  api.os_utils.collect_os_info()
  cache_root = api.path['cache'].join('builder')
  api.repo_util.engine_checkout(cache_root, {}, {})
  with api.context(cwd=cache_root):
    api.goma.ensure()

    android_home = cache_root.join('src', 'third_party', 'android_tools', 'sdk')
    env = {'GOMA_DIR': api.goma.goma_dir, 'ANDROID_HOME': str(android_home)}

    output_files = []
    output_dirs = []
    with api.osx_sdk('ios'), api.depot_tools.on_path(), api.context(
        env=env), api.step.defer_results():
      for build in properties.builds:
        with api.step.nest('build %s (%s)' %
                           (build.dir, ','.join(build.targets))):
          RunGN(api, *build.gn_args)
          Build(api, build.dir, build.disable_goma, *build.targets)
          for output_file in build.output_files:
            output_files.append(
                cache_root.join('src', 'out', build.dir, output_file))
          for output_dir in build.output_dirs:
            output_dirs.append(
                cache_root.join('src', 'out', build.dir, output_dir))
      # This is to clean up leaked processes.
      api.os_utils.kill_processes()
      # Collect memory/cpu/process after task execution.
      api.os_utils.collect_os_info()

    cas_hash = CasOutputs(api, output_files, output_dirs)
    output_props = api.step('Set output properties', None)
    output_props.presentation.properties['cas_output_hash'] = cas_hash


def GenTests(api):
  yield (api.test('Schedule two builds one with goma and one without') +
         api.platform('linux', 64) + api.buildbucket.ci_build(
             builder='Linux Drone',
             git_repo=GIT_REPO,
             project='flutter',
         ) + api.properties(
             InputProperties(
                 mastername='client.flutter',
                 builds=[
                     EngineBuild(
                         disable_goma=True,
                         gn_args=['--unoptimized', '--android'],
                         dir='android_debug_unopt',
                         output_files=['libflutter.so'],
                         output_dirs=['some_dir'],
                     ),
                     EngineBuild(
                         disable_goma=False,
                         gn_args=['--unoptimized'],
                         dir='host_debug_unopt',
                         output_files=['shell_unittests'])
                 ])))
