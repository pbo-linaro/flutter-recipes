# Copyright 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from recipe_engine.post_process import DoesNotRun, Filter, StatusFailure

DEPS = [
    'flutter/build_util',
    'recipe_engine/path',
    'recipe_engine/properties',
]


def RunSteps(api):
  checkout = api.path['start_dir']
  api.build_util.run_gn([], checkout)
  api.build_util.build('release', checkout, ['mytarget'])


def GenTests(api):
  yield api.test('basic', api.properties(no_lto=True, goma_jobs=100))