# Copyright 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from platform import platform
from recipe_engine.recipe_api import Property

DEPS = [
    'flutter/repo_util',
    'flutter/yaml',
    'recipe_engine/buildbucket',
    'recipe_engine/cipd',
    'recipe_engine/context',
    'recipe_engine/json',
    'recipe_engine/path',
    'recipe_engine/platform',
    'recipe_engine/properties',
    'recipe_engine/step',
]


# This recipe builds the codesign CIPD package.
def RunSteps(api):
  start_path = api.path['start_dir']
  cocoon_dir = start_path.join('cocoon')
  cocoon_git_rev = api.repo_util.checkout(
      'cocoon',
      cocoon_dir,
      ref='refs/heads/main',
  )

  # Get properties from ci.yaml.
  # Assume ci.yaml has the following properties
  #   - name: Mac codesign
  #     bringup: true
  #     postsubmit: false
  #     recipe: cocoon/codesign
  #     properties:
  #       add_recipes_cq: "true"
  #     script: device_doctor/tool/build.sh
  #     cipd_name: flutter/device_doctor/mac-amd64

  script_path_list = api.properties.get('script')
  cipd_full_name = api.properties.get('cipd_name')
  project_name = cipd_full_name.split('/')[1]
  project_path = cocoon_dir.join(project_name)

  build_file = cocoon_dir.join(script_path_list)

  cmd = [build_file]
  if not api.platform.is_win:
    cmd = ['bash', build_file]

  with api.context(cwd=project_path):
    api.step('build package', cmd)

    cipd_zip_path = project_name + '.zip'

    api.cipd.build(project_path.join('build'), cipd_zip_path, cipd_full_name)
    if api.buildbucket.build.builder.bucket == 'prod':
      api.cipd.register(cipd_full_name, cipd_zip_path, refs = ["latest"])


def GenTests(api):
  yield api.test(
      'cipd_mac_no_upload',
      api.properties(
          script='codesign/tool/build.sh',
          cipd_name='flutter/codesign/mac-amd64'
      ),
      api.platform('mac', 64),
      api.buildbucket.ci_build(
          git_ref='refs/heads/main',
          bucket='try',
      ),
  )

  yield api.test(
      'cipd_win_upload',
      api.properties(
          script='device_doctor\\tool\\build.bat',
          cipd_name='flutter/device_doctor/windows-amd64'
      ),
      api.platform('win', 64),
      api.buildbucket.ci_build(
          git_ref='refs/heads/main',
          bucket='prod',
      ),
  )
