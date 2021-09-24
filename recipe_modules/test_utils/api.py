# Copyright 2020 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from recipe_engine import recipe_api

# The maximum number of characters to be included in the summary markdown.
# Even though the max size for the markdown is 4000 bytes we are saving 500
# bytes for addittional prefixes added automatically by LUCI like the number
# of failed steps out of the total.
MAX_CHARS = 3500

# Default timeout for tests seconds
TIMEOUT_SECS = 3600


class TestUtilsApi(recipe_api.RecipeApi):
  """Utilities to run flutter tests."""

  def _truncateString(self, string):
    """Truncate the string to MAX_CHARS"""
    byte_count = 0
    lines = string.splitlines()
    output = []
    for line in reversed(lines):
      # +1 to account for the \n separators.
      byte_count += len(line.encode('utf-8')) + 1
      if byte_count >= MAX_CHARS:
        break
      output.insert(0, line)
    return '\n'.join(output)

  def _is_flaky(self, output):
    """Check if test step is flaky"""
    lines = output.splitlines()
    lines.reverse()
    # The flakiness status message `flaky: true` is expected to be located at the
    # end of the stdout file. Check last 10 lines to make sure it is covered if existing.
    for line in lines[:10]:
      if 'flaky: true' in line:
        return True
    return False

  def is_devicelab_bot(self):
    """Whether the current bot is a devicelab bot or not."""
    return (
        str(self.m.swarming.bot_id).startswith('flutter-devicelab') or
        str(self.m.swarming.bot_id).startswith('flutter-win')
    )

  def run_test(self, step_name, command_list, timeout_secs=TIMEOUT_SECS):
    """Recipe's step wrapper to collect stdout and add it to step_summary.

    Args:
      step_name(str): The name of the step.
      command_list(list(str)): A list of strings with the command and
        parameters to execute.
      timeout_secs(int): The timeout in seconds for this step.

    Returns(str): The status of the test step. A str `flaky` or `success` will 
      be returned when step succeeds, and an exception will be thrown out when
      step fails.
    """
    try:
      step = self.m.step(
          step_name,
          command_list,
          stdout=self.m.raw_io.output_text(),
          stderr=self.m.raw_io.output_text(),
          timeout=timeout_secs
      )
    except self.m.step.StepFailure as f:
      result = f.result
      # Truncate stdout
      stdout = self._truncateString(result.stdout)
      # Truncate stderr
      stderr = self._truncateString(result.stderr)
      raise self.m.step.StepFailure('\n\n```%s```\n' % (stdout or stderr))
    finally:
      self.m.step.active_result.presentation.logs[
          'test_stdout'] = self.m.step.active_result.stdout
      self.m.step.active_result.presentation.logs[
          'test_stderr'] = self.m.step.active_result.stderr
    if self._is_flaky(step.stdout):
      test_run_status = 'flaky'
    else:
      test_run_status = 'success'
    return test_run_status

  def test_step_name(self, step_name):
    """Append keyword test to test step name to be consistent.
    Args:
      step_name(str): The name of the step.

    Returns(str): The test step name prefixed with "test".
    """
    return 'test: %s' % step_name

  def flaky_step(self, step_name):
    """Add a flaky step when test is flaky.
    Args:
      step_name(str): The name of the step.
    """
    if self.m.platform.is_win:
      self.m.step(
          'step is flaky: %s' % step_name,
          ['powershell.exe', 'echo "test run is flaky"'],
          infra_step=True,
      )
    else:
      self.m.step(
          'step is flaky: %s' % step_name,
          ['echo', 'test run is flaky'],
          infra_step=True,
      )