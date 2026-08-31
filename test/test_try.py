import base64
import json
import re
from unittest.mock import Mock, patch

from sync import tc, trypush
from sync.lock import SyncLock
from conftest import mach_try_output


def test_read_try_task_config(env, git_gecko):
    try_commit = trypush.TryFuzzyCommit(git_gecko, git_gecko, None, 0, hacks=False)
    message, try_task_config = try_commit.read_try_task_config(
        mach_try_output.decode("utf8", "replace")
    )

    assert message.startswith("Fuzzy query=web-platform-tests")
    assert message.endswith("Pushed via `mach try fuzzy`")
    assert json.loads(try_task_config)["parameters"]["try_task_config"]["tasks"] == [
        "test-linux2404-64/debug-web-platform-tests-1",
        "test-linux2404-64/opt-web-platform-tests-1",
    ]


def test_read_try_rev(env, git_gecko):
    try_commit = trypush.TryFuzzyCommit(git_gecko, git_gecko, None, 0, hacks=False)
    job_id = env.lando.try_push(["patch"], "0" * 40)

    assert try_commit.read_try_rev(job_id) == "%040x" % job_id


def test_try_push_patches(env, try_push):
    assert len(env.lando.try_pushes) == 1
    lando_push = env.lando.try_pushes[0]

    assert lando_push["repo_name"] == "try"
    assert lando_push["patch_format"] == "git-format-patch"
    assert lando_push["base_commit_vcs"] == "hg"
    assert re.match("^[0-9a-f]{40}$", lando_push["base_commit"])

    patches = [base64.b64decode(item).decode("utf8") for item in lando_push["patches"]]
    assert patches
    assert "try_task_config.json" in patches[-1]
    # The try commit is the last one in the push, so its message is the one that gets
    # passed to the decision task
    assert f"wptsync-try-push: {try_push.token}" in patches[-1]


def test_try_task_states(mock_tasks, try_push):
    tasks = Mock(
        return_value=mock_tasks(completed=["foo", "bar"] * 5, failed=["foo", "foo", "bar", "baz"])
    )
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                tasks = try_push.tasks()
                states = tasks.wpt_states()
                assert not tasks.success()
                assert set(states.keys()) == {"baz", "foo", "bar"}
                assert states["foo"]["states"][tc.SUCCESS] == 5
                assert states["foo"]["states"][tc.FAIL] == 2
                assert states["bar"]["states"][tc.SUCCESS] == 5
                assert states["bar"]["states"][tc.FAIL] == 1
                assert states["baz"]["states"][tc.FAIL] == 1
                retriggered_states = tasks.retriggered_wpt_states()
                assert tasks.success_rate() == float(10) / len(tasks)
                # baz is not retriggered, only occurs once
                assert list(retriggered_states.keys()) == ["foo", "bar"]


def test_try_task_states_all_success(mock_tasks, try_push):
    tasks = Mock(return_value=mock_tasks(completed=["foo", "bar"] * 5))
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                tasks = try_push.tasks()
                assert tasks.success()
                assert tasks.success_rate() == 1.0


def test_retrigger_failures(mock_tasks, try_push):
    failed = ["foo", "foo", "bar", "baz", "foo-aarch64"]
    ex = ["bar", "boo", "bork"]
    tasks = Mock(return_value=mock_tasks(completed=["foo", "bar"] * 5, failed=failed, exception=ex))
    retrigger_count = 5
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                with patch(
                    "sync.trypush.auth_tc.retrigger", return_value=["job"] * retrigger_count
                ):
                    tasks = try_push.tasks()
                    jobs = tasks.retrigger_failures(count=retrigger_count)
    assert jobs == retrigger_count * (len(set(failed + ex)) - 1)


def test_download_logs(mock_tasks, try_push):
    failed = ["foo", "foo", "bar", "baz"]
    ex = ["bar", "boo"]
    tasks = Mock(
        return_value=mock_tasks(completed=["foo", "bar", "woo"] * 5, failed=failed, exception=ex)
    )
    with SyncLock.for_process(try_push.process_name) as lock:
        with try_push.as_mut(lock):
            try_push.try_rev = "1" * 40
            with patch.object(tc.TaskGroup, "tasks", property(tasks)):
                with patch.object(tc.TaskGroupView, "download_logs", Mock()):
                    tasks = try_push.tasks()
                    download_tasks = try_push.download_logs(tasks)
                    task_names = [t["task"]["metadata"]["name"] for t in download_tasks]
                    assert task_names.count("foo") == 7
                    assert task_names.count("bar") == 7
                    assert task_names.count("woo") == 5
                    assert task_names.count("boo") == 1
                    assert task_names.count("baz") == 1
                    assert len(task_names) == 21
