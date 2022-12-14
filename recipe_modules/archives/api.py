# Copyright 2022 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import attr
import re

from recipe_engine import recipe_api


@attr.s
class ArchivePaths(object):
  """Paths for an archive config."""
  local = attr.ib(type=str)
  remote = attr.ib(type=str)


DEFAULT_BUCKET = 'flutter_archives_v2'
ANDROID_ARTIFACTS_BUCKET = 'download.flutter.io'
# Used for mock paths
DIRECTORY = 'DIRECTORY'

# Relative paths used to mock paths for testing.
MOCK_JAR_PATH = (
    'io/flutter/x86_debug/'
    '1.0.0-0005149dca9b248663adcde4bdd7c6c915a76584/'
    'x86_debug-1.0.0-0005149dca9b248663adcde4bdd7c6c915a76584.jar'
)
MOCK_POM_PATH = (
    'io/flutter/x86_debug/'
    '1.0.0-0005149dca9b248663adcde4bdd7c6c915a76584/'
    'x86_debug-1.0.0-0005149dca9b248663adcde4bdd7c6c915a76584.pom'
)


class ArchivesApi(recipe_api.RecipeApi):
  """Api to handle archives from engine_v2 recipes."""

  def _full_path_list(self, checkout, archive_config):
    """Calculates the local paths using an archive_config.

    Args:
      checkout: (Path) the checkout path of the engine repository.
      archive_config: (dict) a dictionary with the archive files generated by
        a given build.

    Returns:
      A list of strings with the expected local files as described
      by the archive configuration.
    """
    results = []
    self.m.path.mock_add_paths(
        self.m.path['start_dir'].join(
            'out/android_profile/zip_archives/download.flutter.io'),
        DIRECTORY
    )
    for include_path in archive_config.get('include_paths', []):
      full_include_path = self.m.path.abspath(checkout.join(include_path))
      if self.m.path.isdir(full_include_path):
        test_data = [

        ]
        paths = self.m.file.listdir(
                'Expand directory', checkout.join(include_path),
                recursive=True, test_data=(MOCK_JAR_PATH, MOCK_POM_PATH))
        paths = [self.m.path.abspath(p) for p in paths]
        results.extend(paths)
      else:
        results.append(full_include_path)
    return results

  def _split_dst_parts(self, dst):
    """Splits gsutil uri into a bucket and path sections.

    Args:
      dst: (str) a gcs path like gs://bucket/a/b/c.

    Returns:
      A tuple with the bucket as the first item and the path to the
      object as the second parameter.
    """
    matches = re.match('gs://(\w+)/(.+)', dst)
    return (matches.group(1), matches.group(2))

  def upload_artifact(self, src, dst):
    """Uploads a local object to a gcs destination.

    This method also ensures the directoy structure is recreated in the
    destination.

    Args:
      src: (str) a string with the object local path.
      dst: (str) a string with the destination path in gcs.
    """
    bucket, path = self._split_dst_parts(dst)
    dir_part = self.m.path.dirname(path)
    archive_dir = self.m.path.mkdtemp()
    local_dst_tree = archive_dir.join(*dir_part.split('/'))
    self.m.file.ensure_directory('Ensure %s' % dir_part, local_dst_tree)
    self.m.file.copy('Copy %s' % dst, src, local_dst_tree)
    self.m.gsutil.upload(
        source='%s/*' % archive_dir,
        bucket=bucket,
        dest='',
        args=['-r'],
        name=path,
    )

  def download(self, src, dst):
    """Downloads a file from GCS.

    Args:
      src: A string with gcs uri to download.
      dst: A string with the local destination for the file.
    """
    bucket, path = self._split_dst_parts(src)
    self.m.gsutil.download(
        bucket, path, dst, name="download %s" % src
    )

  def engine_v2_gcs_paths(self, checkout, archive_config, bucket=DEFAULT_BUCKET):
    """Calculates engine v2 GCS paths from an archive config.

    Args:
      checkout: (Path) the engine repository checkout folder.
      archive_config: (dict) the archive configuration for a recipes v2 build.
      bucket: (str) the bucket used to calculate the object destination.

    Returns:
      A list of ArchivePaths with expected local and remote locations for the
      generated artifacts.
    """
    results = []
    # Do not archive if the build is a try build or has no input commit
    if (self.m.buildbucket.build.input.gerrit_changes or
        not self.m.buildbucket.gitiles_commit.project):
      return results

    file_list = self._full_path_list(checkout, archive_config)
    # Calculate prefix and commit.
    is_monorepo = self.m.buildbucket.gitiles_commit.project == 'monorepo'

    if is_monorepo:
      commit = self.m.repo_util.get_commit(checkout.join('../../monorepo'))
      artifact_prefix = 'monorepo/'
    else:
      commit = self.m.repo_util.get_commit(checkout.join('flutter'))
      artifact_prefix = ''

    for include_path in file_list:
      is_android_artifact = ANDROID_ARTIFACTS_BUCKET in include_path
      dir_part = self.m.path.dirname(include_path)
      full_base_path = self.m.path.abspath(checkout.join(archive_config.get('base_path','')))
      rel_path = self.m.path.relpath(dir_part, full_base_path)
      rel_path = '' if rel_path == '.' else rel_path
      base_name = self.m.path.basename(include_path)

      if is_android_artifact:
        # We are not using a slash in the first parameter becase artifact_prefix
        # already includes the slash.
        artifact_path = '%s%s/%s' % (
            artifact_prefix, rel_path, base_name)
      else:
        artifact_path = '%sflutter_infra_release/flutter/%s/%s/%s' % (
            artifact_prefix, commit, rel_path, base_name)

      results.append(
          ArchivePaths(
              include_path,
              'gs://%s/%s' % (bucket, artifact_path)
          )
      )
    return results
