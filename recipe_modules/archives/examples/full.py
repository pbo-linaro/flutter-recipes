# Copyright 2022 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from recipe_engine.post_process import DoesNotRun, Filter, StatusFailure

DEPS = [
    'flutter/archives',
    'flutter/monorepo',
    'recipe_engine/buildbucket',
    'recipe_engine/path',
    'recipe_engine/raw_io',
]


def RunSteps(api):
  checkout = api.path['start_dir']
  config = {
      "name": "android_profile",
      "type": "gcs",
      "base_path": "out/android_profile/zip_archives/",
      "include_paths": [
          "out/android_profile/zip_archives/android-arm-profile/artifacts.zip",
          "out/android_profile/zip_archives/android-arm-profile/linux-x64.zip",
          "out/android_profile/zip_archives/android-arm-profile/symbols.zip",
          "out/android_profile/zip_archives/download.flutter.io"
      ]
  }
  results = api.archives.engine_v2_gcs_paths(checkout, config)
  if not results: return
  api.archives.upload_artifact(results[0].local, results[0].remote)
  api.archives.download(results[0].remote, results[0].local)


def GenTests(api):
  yield api.test(
      'basic',
      api.buildbucket.ci_build(
          project='flutter',
          bucket='prod',
          git_repo='https://flutter.googlesource.com/mirrors/engine',
          git_ref='refs/heads/main'
      ),
      api.step_data(
          'git rev-parse',
          stdout=api.raw_io
          .output_text('12345abcde12345abcde12345abcde12345abcde\n')
      )
  )
  yield api.test(
      'monorepo_ci', api.monorepo.ci_build(),
      api.step_data(
          'git rev-parse',
          stdout=api.raw_io
          .output_text('12345abcde12345abcde12345abcde12345abcde\n')
      )
  )
  yield api.test(
      'monorepo_try',
      api.monorepo.try_build(),
  )
