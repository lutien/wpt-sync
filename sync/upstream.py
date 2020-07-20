from __future__ import absolute_import
import enum
import os
import re
import time
import traceback

import git
import newrelic
from bugsy.errors import BugsyException
from github import GithubException
from mozautomation import commitparser

from . import log
from . import commit as sync_commit
from .base import entry_point
from .downstream import DownstreamSync
from .errors import AbortError
from .env import Environment
from .gitutils import update_repositories, gecko_repo
from .gh import AttrDict
from .lock import SyncLock, constructor, mut
from .sync import CommitFilter, LandableStatus, SyncProcess, CommitRange
from .repos import pygit2_get
from six import iteritems, itervalues

MYPY = False
if MYPY:
    from git.repo.base import Repo
    from typing import Text
    from typing import Optional
    from sync.commit import GeckoCommit
    from typing import List
    from sync.commit import Commit
    from typing import Tuple
    from sync.base import ProcessName
    from typing import Any
    from typing import Set
    from typing import Dict
    from typing import Union

    CreateSyncs = Dict[Optional[str], Union[List, "Endpoints"]]
    UpdateSyncs = Dict[str, Tuple["UpstreamSync", GeckoCommit]]


env = Environment()

logger = log.get_logger(__name__)


class BackoutCommitFilter(CommitFilter):
    def __init__(self, bug_id):
        # type: (str) -> None
        self.bug = bug_id
        self.seen = set()
        self._commits = {}

    def _filter_commit(self, commit):
        # type: (GeckoCommit) -> bool
        if commit.metadata.get("wptsync-skip"):
            return False
        if DownstreamSync.has_metadata(commit.msg):
            return False
        if commit.is_backout:
            commits, _ = commit.wpt_commits_backed_out()
            for backout_commit in commits:
                if backout_commit.sha1 in self.seen:
                    return True
        if commit.bug == self.bug:
            if commit.is_empty(env.config["gecko"]["path"]["wpt"]):
                return False
            self.seen.add(commit.sha1)
            return True
        return False

    def filter_commits(self, commits):
        # type: (List[GeckoCommit]) -> List[GeckoCommit]
        return remove_complete_backouts(commits)


class UpstreamSync(SyncProcess):
    sync_type = "upstream"
    obj_id = "bug"
    statuses = ("open", "wpt-merged", "complete", "incomplete")
    status_transitions = [("open", "wpt-merged"),
                          ("open", "complete"),
                          ("open", "incomplete"),
                          ("incomplete", "open"),
                          ("wpt-merged", "complete")]
    multiple_syncs = True

    def __init__(self, git_gecko, git_wpt, process_name):
        # type: (Repo, Repo, ProcessName) -> None
        super(UpstreamSync, self).__init__(git_gecko, git_wpt, process_name)

        self._upstreamed_gecko_commits = None
        self._upstreamed_gecko_head = None

    @classmethod
    @constructor(lambda args: ("upstream", args['bug']))
    def new(cls,
            lock,  # type: SyncLock
            git_gecko,  # type: Repo
            git_wpt,  # type: Repo
            gecko_base,  # type: Text
            gecko_head,  # type: str
            wpt_base="origin/master",  # type: str
            wpt_head=None,  # type: str
            bug=None,  # type: str
            status="open",  # type: str
            ):
        # type: (...) -> UpstreamSync
        self = super(UpstreamSync, cls).new(lock,
                                            git_gecko,
                                            git_wpt,
                                            gecko_base,
                                            gecko_head,
                                            wpt_base=wpt_base,
                                            wpt_head=wpt_head,
                                            bug=bug,
                                            status=status)
        with self.as_mut(lock):
            for commit in self.gecko_commits:
                commit.set_upstream_sync(self)
        return self

    @classmethod
    def from_pr(cls, lock, git_gecko, git_wpt, pr_id, body):
        gecko_commits = []
        bug = None
        integration_branch = None

        if not cls.has_metadata(body):
            return None

        commits = env.gh_wpt.get_commits(pr_id)

        for gh_commit in commits:
            commit = sync_commit.WptCommit(git_wpt, gh_commit.sha)
            if cls.has_metadata(commit.message):
                gecko_commits.append(git_gecko.cinnabar.hg2git(commit.metadata["gecko-commit"]))
                commit_bug = env.bz.id_from_url(commit.metadata["bugzilla-url"])
                if bug is not None and commit_bug != bug:
                    logger.error("Got multiple bug numbers in URL from commits")
                    break
                elif bug is None:
                    bug = commit_bug

                if (integration_branch is not None and
                    commit.metadata["integration_branch"] != integration_branch):
                    logger.warning("Got multiple integration branches from commits")
                elif integration_branch is None:
                    integration_branch = commit.metadata["integration_branch"]
            else:
                break

        if not gecko_commits:
            return None

        assert bug
        gecko_base = git_gecko.rev_parse("%s^" % gecko_commits[0])
        gecko_head = git_gecko.rev_parse(gecko_commits[-1])
        wpt_head = commits[-1].sha
        wpt_base = commits[0].sha

        return cls.new(lock, git_gecko, git_wpt, gecko_base, gecko_head,
                       wpt_base, wpt_head, bug, pr_id)

    @classmethod
    def has_metadata(cls, message):
        # type: (Text) -> bool
        required_keys = ["gecko-commit",
                         "gecko-integration-branch",
                         "bugzilla-url"]
        metadata = sync_commit.get_metadata(message)
        return all(item in metadata for item in required_keys)

    def gecko_commit_filter(self):
        # type: () -> BackoutCommitFilter
        return BackoutCommitFilter(self.bug)

    @property
    def landable_status(self):
        return LandableStatus.upstream

    @property
    def bug(self):
        # type: () -> str
        return self.process_name.obj_id

    @property
    def pr_status(self):
        return self.data.get("pr-status", "open")

    @pr_status.setter
    def pr_status(self, value):
        self.data["pr-status"] = value

    @property
    def merge_sha(self):
        # type: () -> Text
        return self.data.get("merge-sha", None)

    @merge_sha.setter
    def merge_sha(self, value):
        # type: (Optional[Text]) -> None
        self.data["merge-sha"] = value

    @property
    def remote_branch(self):
        # type: () -> Optional[str]
        return self.data.get("remote-branch")

    @remote_branch.setter
    @mut()
    def remote_branch(self, value):
        # type: (Optional[str]) -> None
        if value:
            assert not value.startswith("refs/")
        self.data["remote-branch"] = value

    @mut()
    def get_or_create_remote_branch(self):
        # type: () -> Text
        if not self.remote_branch:
            pygit2_gecko = pygit2_get(self.git_gecko)
            pygit2_wpt = pygit2_get(self.git_wpt)
            if self.branch_name in pygit2_gecko.branches:
                upstream = pygit2_gecko.branches[self.branch_name].upstream
                if upstream:
                    self.remote_branch = upstream.shortname

        if not self.remote_branch:
            count = 0
            refs = pygit2_wpt.references
            initial_path = path = "refs/remotes/origin/gecko/%s" % self.bug
            while path in refs:
                count += 1
                path = "%s-%s" % (initial_path, count)
            self.remote_branch = path[len("refs/remotes/origin/"):]
        return self.remote_branch

    @property
    def upstreamed_gecko_commits(self):
        # type: () -> List[GeckoCommit]
        if (self._upstreamed_gecko_commits is None or
            self._upstreamed_gecko_head != self.wpt_commits.head.sha1):
            self._upstreamed_gecko_commits = [
                sync_commit.GeckoCommit(self.git_gecko,
                                        self.git_gecko.cinnabar.hg2git(
                                            wpt_commit.metadata["gecko-commit"]))
                for wpt_commit in self.wpt_commits
                if "gecko-commit" in wpt_commit.metadata]
            self._upstreamed_gecko_head = self.wpt_commits.head.sha1
        return self._upstreamed_gecko_commits

    @mut()
    def update_wpt_commits(self):
        # type: () -> bool
        matching_commits = []

        if len(self.gecko_commits) == 0:
            return False

        # Find the commits that were already upstreamed. Some gecko commits may not
        # result in an upstream commit, if the patch has no effect. But if we find
        # the last commit that was previously upstreamed then all earlier ones must
        # also match.
        upstreamed_commits = {item.sha1 for item in self.upstreamed_gecko_commits}
        matching_commits = self.gecko_commits[:]
        for gecko_commit in reversed(self.gecko_commits):
            if gecko_commit.sha1 in upstreamed_commits:
                break
            matching_commits.pop()

        if len(matching_commits) == len(self.gecko_commits) == len(self.upstreamed_gecko_commits):
            return False

        if len(matching_commits) == 0:
            self.wpt_commits.head = self.wpt_commits.base
        elif len(matching_commits) < len(self.upstreamed_gecko_commits):
            self.wpt_commits.head = self.wpt_commits[len(matching_commits) - 1]

        # Ensure the worktree is clean
        wpt_work = self.wpt_worktree.get()
        wpt_work.git.reset(hard=True)
        wpt_work.git.clean(f=True, d=True, x=True)

        for commit in self.gecko_commits[len(matching_commits):]:
            commit = self.add_commit(commit)

        assert (len(self.wpt_commits) ==
                len(self.upstreamed_gecko_commits))

        return True

    def gecko_landed(self):
        # type: () -> bool
        if not len(self.gecko_commits):
            return False
        landed = [self.git_gecko.is_ancestor(commit.sha1, env.config["gecko"]["refs"]["central"])
                  for commit in self.gecko_commits]
        if not all(item == landed[0] for item in landed):
            logger.warning("Got some commits landed and some not for upstream sync %s" %
                           self.branch_name)
            return False
        return landed[0]

    @property
    def repository(self):
        # type: () -> str
        # Need to check central before landing repos
        head = self.gecko_commits.head.sha1
        repo = gecko_repo(self.git_gecko, head)
        if repo is None:
            raise ValueError("Commit %s not part of any repository" % head)
        return repo

    @mut()
    def add_commit(self, gecko_commit):
        # type: (GeckoCommit) -> Tuple[Optional[Commit], bool]
        git_work = self.wpt_worktree.get()

        metadata = {"gecko-commit": gecko_commit.canonical_rev,
                    "gecko-integration-branch": self.repository}

        if os.path.exists(os.path.join(git_work.working_dir, gecko_commit.canonical_rev + ".diff")):
            # If there's already a patch file here then don't try to create a new one
            # because we'll presumbaly fail again
            raise AbortError("Skipping due to existing patch")
        wpt_commit = gecko_commit.move(git_work,
                                       metadata=metadata,
                                       msg_filter=commit_message_filter,
                                       src_prefix=env.config["gecko"]["path"]["wpt"])
        assert not git_work.is_dirty()
        if wpt_commit:
            self.wpt_commits.head = wpt_commit

        return wpt_commit, True

    @mut()
    def create_pr(self):
        # type: () -> int
        if self.pr:
            return self.pr

        assert self.remote_branch is not None
        assert self.remote_branch in self.git_wpt.remotes.origin.refs
        while not env.gh_wpt.get_branch(self.remote_branch):
            logger.debug("Waiting for branch")
            time.sleep(1)

        commit_summary = self.wpt_commits[0].commit.summary

        body = self.wpt_commits[0].msg.split("\n", 1)
        body = body[1] if len(body) != 1 else ""

        pr_id = env.gh_wpt.create_pull(
            title="[Gecko%s] %s" % (" Bug %s" % self.bug if self.bug else "", commit_summary),
            body=body.strip(),
            base="master",
            head=self.remote_branch)
        self.pr = pr_id
        # TODO: add label to bug
        env.bz.comment(self.bug,
                       "Created web-platform-tests PR %s for changes under "
                       "testing/web-platform/tests" %
                       env.gh_wpt.pr_url(pr_id))
        return pr_id

    @mut()
    def push_commits(self):
        # type: () -> None
        remote_branch = self.get_or_create_remote_branch()
        logger.info("Pushing commits from bug %s to branch %s" % (self.bug, remote_branch))
        push_info = self.git_wpt.remotes.origin.push("refs/heads/%s:%s" %
                                                     (self.branch_name, remote_branch),
                                                     force=True,
                                                     set_upstream=True)
        for item in push_info:
            if item.flags & item.ERROR:
                raise AbortError(item.summary)

    def push_required(self):
        # type: () -> bool
        return not (self.remote_branch and
                    self.remote_branch in self.git_wpt.remotes.origin.refs and
                    self.git_wpt.remotes.origin.refs[self.remote_branch].commit.hexsha ==
                    self.wpt_commits.head.sha1)

    @mut()
    def update_github(self):
        # type: () -> None
        if self.pr:
            state = env.gh_wpt.pull_state(self.pr)
            if not len(self.gecko_commits):
                env.gh_wpt.close_pull(self.pr)
            elif state == "closed":
                pr = env.gh_wpt.get_pull(self.pr)
                if not pr.merged:
                    env.gh_wpt.reopen_pull(self.pr)
                else:
                    # If all the local commits are represented upstream, everything is
                    # fine and close out the sync. Otherwise we have a problem.
                    if len(self.upstreamed_gecko_commits) == len(self.gecko_commits):

                        if self.status not in ("wpt-merged", "complete"):
                            env.bz.comment(self.bug, "Upstream PR merged")

                        self.finish()
                    else:
                        # It's unclear what to do in this case, so mark the sync for manual
                        # fixup
                        self.error = "Upstream PR merged, but additional commits added after merge"
                    return

        if not len(self.gecko_commits):
            return

        if not len(self.upstreamed_gecko_commits):
            return

        if self.push_required():
            self.push_commits()
        if not self.pr:
            self.create_pr()

        self.set_landed_status()

    def set_landed_status(self):
        # type: () -> None
        """
        Set the status of the check on the GitHub commit upstream. This check
        is used to tell if the code has been landed into Gecko.
        """
        if not self.pr:
            return
        landed_status = "success" if self.gecko_landed() else "failure"
        logger.info("Setting landed status to %s" % landed_status)
        # TODO - Maybe ignore errors setting the status
        env.gh_wpt.set_status(self.pr,
                              landed_status,
                              target_url=env.bz.bugzilla_url(self.bug),
                              description="Landed on mozilla-central",
                              context="upstream/gecko")

    @mut()
    def try_land_pr(self):
        # type: () -> bool
        logger.info("Checking if sync for bug %s can land" % self.bug)
        if not self.status == "open":
            logger.info("Sync is %s" % self.status)
            return
        if not self.gecko_landed():
            logger.info("Commits are not yet landed in gecko")
            return False

        if not self.pr:
            logger.info("No upstream PR created")
            return False

        self.set_landed_status()

        merge_sha = env.gh_wpt.merge_sha(self.pr)
        if merge_sha:
            logger.info("PR already merged")
            self.merge_sha = merge_sha
            self.finish("wpt-merged")
            return

        logger.info("Commit are landable; trying to land %s" % self.pr)

        msg = None
        check_status, checks = get_check_status(self.pr)
        if check_status not in [CheckStatus.SUCCESS, CheckStatus.PENDING]:
            details = ["Github PR %s" % env.gh_wpt.pr_url(self.pr)]
            msg = ("Can't merge web-platform-tests PR due to failing upstream checks:\n%s" %
                   details)
        elif not env.gh_wpt.is_mergeable(self.pr):
            msg = "Can't merge web-platform-tests PR because it has merge conflicts"
        elif not env.gh_wpt.is_approved(self.pr):
            # This should be handled by the pr-bot
            msg = "Can't merge web-platform-tests PR because it is missing approval"
        else:
            try:
                merge_sha = env.gh_wpt.merge_pull(self.pr)
                env.bz.comment(self.bug, "Upstream PR merged by %s" %
                               env.config["web-platform-tests"]["github"]["user"])
            except GithubException as e:
                msg = ("Merging PR %s failed.\nMessage: %s" %
                       (env.gh_wpt.pr_url(self.pr),
                        e.data.get("message", "Unknown GitHub Error")))
            except Exception as e:
                msg = ("Merging PR %s failed.\nMessage: %s" %
                       (env.gh_wpt.pr_url(self.pr), e.message))
            else:
                self.merge_sha = merge_sha
                self.finish("wpt-merged")
                return True
        if msg is not None:
            logger.error(msg)
        return False

    @mut()
    def finish(self, status="complete"):
        # type: (str) -> None
        super(UpstreamSync, self).finish(status)
        if status in ("wpt-merged", "complete") and self.remote_branch:
            # Delete the remote branch after a merge
            try:
                self.git_wpt.remotes.origin.push(self.remote_branch, delete=True)
            except git.GitCommandError:
                pass
            else:
                self.remote_branch = None

    @property
    def pr_head(self):
        # type: () -> Text
        """
        Retrieves the head of the PR ref: origin/pr/{pr_id}
        :return: The SHA of the head commit.
        """
        if not self.pr:
            logger.error("No PR ID found for %s" % self.process_name)
            return

        pr_ref = 'origin/pr/{}'.format(self.pr)

        if pr_ref not in self.git_wpt.refs:
            # PR ref doesn't seem to exist
            logger.error("No ref found for %s" % pr_ref)
            return

        ref = self.git_wpt.refs[pr_ref]
        return ref.commit.hexsha

    @property
    def pr_commits(self):
        # type: () -> CommitRange
        pr_head = self.pr_head
        if not pr_head:
            raise ValueError("Can't get PR commits as the ref head could not be found for %s" %
                             self.process_name)
        else:
            pr_head = sync_commit.WptCommit(self.git_wpt, pr_head)

        merge_base = []

        # Check if the PR Head is reachable from origin/master
        origin_master_sha = self.git_wpt.refs['origin/master'].commit.hexsha
        pr_head_reachable = self.git_wpt.is_ancestor(pr_head.sha1, 'origin/master')

        # If not reachable, then it either hasn't landed yet, it was a Squash + Merge,
        # or a Rebase and merge.
        if not pr_head_reachable:
            merge_base = self.git_wpt.merge_base(origin_master_sha, pr_head.sha1)
        else:
            if not self.merge_sha:
                raise ValueError('The merge SHA for %s could not be found in the UpstreamSync' %
                                 self.process_name)
            merge_commit = sync_commit.WptCommit(self.git_wpt, self.merge_sha)

            # If the commit has two parents, one of them being our pr head, it is a merge commit
            parents = list(merge_commit.commit.parents)
            if len(parents) == 2 and pr_head in parents:
                other_parent = parents[0] if parents[1] == pr_head.commit else parents[1]
                merge_base = self.git_wpt.merge_base(pr_head.sha1, other_parent)

            # Not a merge commit, so just use the base we have stored
            else:
                merge_base = [self.wpt_commits.base.commit]

        # Check that we found the merge base
        if len(merge_base) == 0:
            raise ValueError("Problem determining merge base for %s" % self.process_name)
        else:
            merge_base = merge_base[0]

        # Create a CommitRange object and return it
        base = sync_commit.WptCommit(self.git_wpt, merge_base)
        head_ref = AttrDict({'commit': pr_head})
        return CommitRange(self.git_wpt, base, head_ref, sync_commit.WptCommit, CommitFilter())


def commit_message_filter(msg):
    # type: (Text) -> Tuple[Text, Dict[str, Text]]
    metadata = {}
    m = commitparser.BUG_RE.match(msg)
    if m:
        bug_str, bug_number = m.groups()[:2]
        if msg.startswith(bug_str):
            prefix = re.compile(r"^%s[^\w\d\[\(]*" % bug_str)
            msg = prefix.sub("", msg)
        metadata["bugzilla-url"] = env.bz.bugzilla_url(bug_number)

    reviewers = ", ".join(commitparser.parse_reviewers(msg))
    if reviewers:
        metadata["gecko-reviewers"] = reviewers
    msg = commitparser.replace_reviewers(msg, "")
    msg = commitparser.strip_commit_metadata(msg)
    description = msg.splitlines()
    if description:
        summary = description.pop(0)
        summary = summary.rstrip("!#$%&(*+,-/:;<=>@[\\^_`{|~").rstrip()
        description = "\n".join(description)
        msg = summary + ("\n" + description if description else "")

    return msg, metadata


def wpt_commits(git_gecko, first_commit, head_commit):
    # type: (Repo, GeckoCommit, GeckoCommit) -> List[GeckoCommit]
    # List of syncs that have changed, so we can update them all as appropriate at the end
    revish = "%s..%s" % (first_commit.sha1, head_commit.sha1)
    logger.info("Getting commits in range %s" % revish)
    commits = [sync_commit.GeckoCommit(git_gecko, item.hexsha) for item in
               git_gecko.iter_commits(revish,
                                      paths=env.config["gecko"]["path"]["wpt"],
                                      reverse=True,
                                      max_parents=1)]
    return filter_commits(commits)


def filter_commits(commits):
    # type: (List[GeckoCommit]) -> List[GeckoCommit]
    rv = []
    for commit in commits:
        if (commit.metadata.get("wptsync-skip") or
            DownstreamSync.has_metadata(commit.msg) or
            (commit.is_backout and not commit.wpt_commits_backed_out()[0])):
            continue
        rv.append(commit)
    return rv


def remove_complete_backouts(commits):
    # type: (List[GeckoCommit]) -> List[GeckoCommit]
    """Given a list of commits, remove any commits for which a backout exists
    in the list"""
    commits_remaining = set()
    for commit in commits:
        if commit.is_backout:
            backed_out, _ = commit.wpt_commits_backed_out()
            backed_out = {item.sha1 for item in backed_out}
            if backed_out.issubset(commits_remaining):
                commits_remaining -= backed_out
                continue
        commits_remaining.add(commit.sha1)

    return [item for item in commits if item.sha1 in commits_remaining]


class Endpoints(object):
    def __init__(self, first):
        # type: (GeckoCommit) -> None
        self._first = first
        self._second = None

    @property
    def base(self):
        # type: () -> Text
        return self._first.commit.parents[0].hexsha

    @property
    def head(self):
        # type: () -> str
        if self._second is not None:
            return self._second.sha1
        return self._first.sha1

    @head.setter
    def head(self, value):
        # type: (GeckoCommit) -> None
        self._second = value

    def __repr__(self):
        return "<Endpoints %s:%s>" % (self.base, self.head)


def updates_for_backout(git_gecko,  # type: Repo
                        git_wpt,  # type: Repo
                        commit,  # type: GeckoCommit
                        ):
    # type: (...) -> Tuple[Dict, Dict[str, Tuple[UpstreamSync, GeckoCommit]]]
    backed_out_commits, bugs = commit.wpt_commits_backed_out()
    backed_out_commit_shas = {item.sha1 for item in backed_out_commits}

    create_syncs = {None: []}
    update_syncs = {}

    for backed_out_commit in backed_out_commits:
        syncs = UpstreamSync.for_bug(git_gecko, git_wpt, backed_out_commit.bug,
                                     statuses={"open", "incomplete"}, flat=True)
        if len(syncs) not in (0, 1):
            raise ValueError("Lookup of upstream syncs for bug %s returned syncs: %r" %
                             (len(syncs), syncs))
        if syncs:
            sync = syncs.pop()
            if commit in sync.gecko_commits:
                # This commit was already processed
                backed_out_commit_shas = set()
                return {}, {}
            if backed_out_commit in sync.upstreamed_gecko_commits:
                backed_out_commit_shas.remove(backed_out_commit.sha1)
                update_syncs[sync.bug] = (sync, commit)

    if backed_out_commit_shas:
        # This backout covers something other than known open syncs, so we need to
        # create a new sync especially for it
        # TODO: we should check for this already existing before we process the backout
        # Need to create a bug for this backout
        backout_bug = None
        for bug in bugs:
            open_bug_syncs = UpstreamSync.for_bug(git_gecko, git_wpt, bug,
                                                  statuses={"open", "incomplete"})
            if bug not in update_syncs and not open_bug_syncs:
                backout_bug = bug
                break
        if backout_bug is None:
            create_syncs[None].append(Endpoints(commit))
        else:
            create_syncs[backout_bug] = Endpoints(commit)
    return create_syncs, update_syncs


def updated_syncs_for_push(git_gecko,  # type: Repo
                           git_wpt,  # type: Repo
                           first_commit,  # type: GeckoCommit
                           head_commit,  # type: GeckoCommit
                           ):
    # type: (...) -> Optional[Tuple[CreateSyncs, UpdateSyncs]]
    # TODO: Check syncs with pushes that no longer exist on autoland
    commits = wpt_commits(git_gecko, first_commit, head_commit)
    if not commits:
        logger.info("No new commits affecting wpt found")
        return
    else:
        logger.info("Got %i commits since the last sync point" % len(commits))

    commits = remove_complete_backouts(commits)

    if not commits:
        logger.info("No commits remain after removing backout pairs")
        return

    create_syncs = {None: []}
    update_syncs = {}

    for commit in commits:
        if commit.upstream_sync(git_gecko, git_wpt) is not None:
            # This commit was already processed e.g. by a manual invocation, so skip
            continue
        if commit.is_backout:
            create, update = updates_for_backout(git_gecko, git_wpt, commit)
            create_syncs.update(create)
            update_syncs.update(update)
        if commit.is_downstream or commit.is_landing:
            continue
        else:
            bug = commit.bug
            if bug in update_syncs:
                sync, _ = update_syncs[bug]
            else:
                statuses = ["open", "incomplete"]
                syncs = UpstreamSync.for_bug(git_gecko, git_wpt, bug, statuses=statuses,
                                             flat=True)
                sync = None
                if len(syncs) not in (0, 1):
                    logger.warning("Lookup of upstream syncs for bug %s returned syncs: %r" %
                                   (len(syncs), syncs))
                    # Try to pick the most recent sync
                    for status in ["open", "incomplete"]:
                        status_syncs = [s for s in syncs if s.status == status]
                        if status_syncs:
                            status_syncs.sort(key=lambda x: int(x.process_name.obj_id))
                            sync = status_syncs.pop()
                            break
                if syncs:
                    sync = syncs[0]

            if sync:
                if isinstance(sync, UpstreamSync) and commit not in sync.gecko_commits:
                    update_syncs[bug] = (sync, commit)
                elif sync.pr is None:
                    update_syncs[bug] = (sync, sync.gecko_commits.head)
            else:
                if bug is None:
                    create_syncs[None].append(Endpoints(commit))
                elif bug in create_syncs:
                    create_syncs[bug].head = commit
                else:
                    create_syncs[bug] = Endpoints(commit)

    return create_syncs, update_syncs


def create_syncs(lock,  # type: SyncLock
                 git_gecko,  # type: Repo
                 git_wpt,  # type: Repo
                 create_endpoints,  # type: Dict[Optional[str], Union[List, Endpoints]]
                 ):
    # type: (...) -> List[UpstreamSync]
    rv = []
    for bug, endpoints in iteritems(create_endpoints):
        if bug is not None:
            endpoints = [endpoints]
        for endpoint in endpoints:
            if bug is None:
                # TODO: Loading the commits doesn't work in this case, because we depend on the bug
                commit = sync_commit.GeckoCommit(git_gecko, endpoint.head)
                bug = env.bz.new("Upstream commit %s to web-platform-tests" %
                                 commit.canonical_rev,
                                 "",
                                 "Testing",
                                 "web-platform-tests",
                                 whiteboard="[wptsync upstream]")
            sync = UpstreamSync.new(lock,
                                    git_gecko,
                                    git_wpt,
                                    bug=bug,
                                    gecko_base=endpoint.base,
                                    gecko_head=endpoint.head,
                                    wpt_base="origin/master",
                                    wpt_head="origin/master")
            rv.append(sync)
    return rv


def update_sync_heads(lock,  # type: SyncLock
                      syncs_by_bug,  # type: Dict[str, Tuple[UpstreamSync, GeckoCommit]]
                      ):
    # type: (...) -> List[UpstreamSync]
    rv = []
    for bug, (sync, commit) in iteritems(syncs_by_bug):
        if sync.status not in ("open", "incomplete"):
            # TODO: Create a new sync with a non-zero seq-id in this case
            raise ValueError("Tried to modify a closed sync for bug %s with commit %s" %
                             (bug, commit.canonical_rev))
        with sync.as_mut(lock):
            sync.gecko_commits.head = commit
            for commit in sync.gecko_commits:
                commit.set_upstream_sync(sync)
        rv.append(sync)
    return rv


def update_modified_sync(git_gecko, git_wpt, sync):
    # type: (Repo, Repo, UpstreamSync) -> None
    assert sync._lock is not None
    if len(sync.gecko_commits) == 0:
        # In the case that there are no gecko commits, we presumably had a backout
        # In this case we don't touch the wpt commits, but just mark the PR
        # as closed. That's pretty counterintuitive, but it turns out that GitHub
        # will only let you reopen a closed PR if you don't change the branch head in
        # the meantime. So we carefully avoid touching the wpt side until something
        # relands and we have a chance to reopen the PR
        logger.info("Sync has no commits, so marking as incomplete")
        sync.status = "incomplete"
        if not sync.pr:
            logger.info("Sync was already fully applied upstream, not creating a PR")
            return
    else:
        sync.status = "open"
        try:
            sync.update_wpt_commits()
        except AbortError:
            # If we got a merge conflict and the PR doesn't exist yet then try
            # recreating the commits on top of the current sync point in order that
            # we get a PR and it's visible that it fails
            if not sync.pr:
                logger.info("Applying to origin/master failed; "
                            "retrying with the current sync point")
                from .landing import load_sync_point
                sync_point = load_sync_point(git_gecko, git_wpt)
                sync.set_wpt_base(sync_point["upstream"])
                try:
                    sync.update_wpt_commits()
                except AbortError:
                    # Reset the base to origin/master
                    sync.set_wpt_base("origin/master")
                    with env.bz.bug_ctx(sync.bug) as bug:
                        bug.add_comment("Failed to create upstream wpt PR due to "
                                        "merge conflicts. This requires fixup from a wpt sync "
                                        "admin.")
                        needinfo_users = [item.strip() for item in
                                          (env.config["gecko"]["needinfo"]
                                           .get("upstream", "")
                                           .split(","))]
                        needinfo_users = [item for item in needinfo_users if item]
                        bug.needinfo(*needinfo_users)
                    raise

    sync.update_github()


def update_sync_prs(lock,  # type: SyncLock
                    git_gecko,  # type: Repo
                    git_wpt,  # type: Repo
                    create_endpoints,  # type: Dict[Optional[str], Union[List, Endpoints]]
                    update_syncs,  # type: Dict[str, Tuple[UpstreamSync, GeckoCommit]]
                    raise_on_error=False,  # type: bool
                    ):
    # type: (...) -> Tuple[Set[UpstreamSync], Set]
    pushed_syncs = set()
    failed_syncs = set()

    to_push = create_syncs(lock, git_gecko, git_wpt, create_endpoints)
    to_push.extend(update_sync_heads(lock, update_syncs))

    for sync in to_push:
        with sync.as_mut(lock):
            try:
                update_modified_sync(git_gecko, git_wpt, sync)
            except Exception as e:
                sync.error = e
                if raise_on_error:
                    raise
                traceback.print_exc()
                logger.error(e)
                failed_syncs.add((sync, e))
            else:
                sync.error = None
                pushed_syncs.add(sync)

    return pushed_syncs, failed_syncs


def try_land_syncs(lock, syncs):
    # type: (SyncLock, Set[UpstreamSync]) -> Set[UpstreamSync]
    landed_syncs = set()
    for sync in syncs:
        with sync.as_mut(lock):
            if sync.try_land_pr():
                landed_syncs.add(sync)
    return landed_syncs


@entry_point("upstream")
@mut('sync')
def update_sync(git_gecko, git_wpt, sync, raise_on_error=True, repo_update=True):
    if sync.status in ("wpt-merged", "complete"):
        logger.info("Nothing to do for sync with status %s" % sync.status)
        return set(), set(), set()

    if repo_update:
        update_repositories(git_gecko, git_wpt)
    assert isinstance(sync, UpstreamSync)
    update_syncs = {sync.bug: (sync, sync.gecko_commits.head.sha1)}
    pushed_syncs, failed_syncs = update_sync_prs(sync._lock,
                                                 git_gecko,
                                                 git_wpt,
                                                 {},
                                                 update_syncs,
                                                 raise_on_error=raise_on_error)

    if sync not in failed_syncs:
        landed_syncs = try_land_syncs(sync._lock, [sync])
    else:
        landed_syncs = set()

    return pushed_syncs, failed_syncs, landed_syncs


@entry_point("upstream")
def gecko_push(git_gecko,  # type: Repo
               git_wpt,  # type: Repo
               repository_name,  # type: str
               hg_rev,  # type: str
               raise_on_error=False,  # type: bool
               base_rev=None,  # type: Optional[Any]
               ):
    # type: (...) -> Tuple[Set[UpstreamSync], Set[UpstreamSync], Set]
    rev = git_gecko.cinnabar.hg2git(hg_rev)
    last_sync_point, prev_commit = UpstreamSync.prev_gecko_commit(git_gecko,
                                                                  repository_name)

    if base_rev is None and git_gecko.is_ancestor(rev, last_sync_point.commit.sha1):
        logger.info("Last sync point moved past commit")
        return

    with SyncLock("upstream", None) as lock:
        updated = updated_syncs_for_push(git_gecko,
                                         git_wpt,
                                         prev_commit,
                                         sync_commit.GeckoCommit(git_gecko, rev))

        if updated is None:
            return set(), set(), set()

        create_endpoints, update_syncs = updated

        pushed_syncs, failed_syncs = update_sync_prs(lock,
                                                     git_gecko,
                                                     git_wpt,
                                                     create_endpoints,
                                                     update_syncs,
                                                     raise_on_error=raise_on_error)

        landable_syncs = {item for item in UpstreamSync.load_by_status(git_gecko, git_wpt, "open")
                          if item.error is None}
        landed_syncs = try_land_syncs(lock, landable_syncs)

        # TODO
        if not git_gecko.is_ancestor(rev, last_sync_point.commit.sha1):
            with last_sync_point.as_mut(lock):
                last_sync_point.commit = rev

    return pushed_syncs, landed_syncs, failed_syncs


@enum.unique
class CheckStatus(enum.Enum):
    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"


def get_check_status(pr_id):
    checks = env.gh_wpt.get_check_runs(pr_id)
    if commit_checks_pass(checks):
        status = CheckStatus.SUCCESS
    elif not commit_checks_complete(checks):
        status = CheckStatus.PENDING
    else:
        status = CheckStatus.FAILURE
    return status, checks


def commit_checks_pass(checks):
    """Boolean indicating whether all required check runs pass"""
    return all(item["required"] is False or (item["status"] == "completed" and
                                             item["conclusion"] in ("success", "neutral"))
               for item in itervalues(checks))


def commit_checks_complete(checks):
    """Boolean indicating whether all check runs are complete"""
    return all(item["status"] == "completed" for item in itervalues(checks))


@entry_point("upstream")
@mut('sync')
def commit_check_changed(git_gecko, git_wpt, sync):
    landed = False
    if sync.status != "open":
        return True

    check_status, checks = get_check_status(sync.pr)

    if not checks:
        logger.error("No checks found for pr %s" % sync.pr)
        return

    # Record the overall status and commit so we only notify once per commit
    this_pr_check = {"state": check_status.value,
                     "sha": itervalues(checks).next()["head_sha"]}
    last_pr_check = sync.last_pr_check
    sync.last_pr_check = this_pr_check

    if check_status == CheckStatus.SUCCESS:
        sync.error = None
        if sync.gecko_landed():
            landed = sync.try_land_pr()
        elif this_pr_check != last_pr_check:
            env.bz.comment(sync.bug,
                           "Upstream web-platform-tests status checks passed, "
                           "PR will merge once commit reaches central.")
    elif check_status == CheckStatus.FAILURE and last_pr_check != this_pr_check:
        details = ["Github PR %s" % env.gh_wpt.pr_url(sync.pr)]
        for name, check_run in iteritems(checks):
            if check_run["conclusion"] not in ("success", "neutral"):
                details.append("* %s (%s)" % (name, check_run["url"]))
        details = "\n".join(details)
        msg = ("Can't merge web-platform-tests PR due to failing upstream checks:\n%s" %
               details)
        try:
            with env.bz.bug_ctx(sync.bug) as bug:
                bug["comment"] = msg
            # Do this as a seperate operation
            with env.bz.bug_ctx(sync.bug) as bug:
                commit_author = sync.gecko_commits[0].email
                if commit_author:
                    bug.needinfo(commit_author)
        except BugsyException:
            msg = traceback.format_exc()
            logger.warning("Failed to update bug:\n%s" % msg)
            # Sometimes needinfos fail because emails addresses in bugzilla don't
            # match the commits. That's non-fatal, but record the exception here in
            # case something more unexpected happens
            newrelic.agent.record_exception()
            sync.error = "Checks failed"
        else:
            logger.info("Some upstream web-platform-tests status checks still pending.")
    return landed


@entry_point("upstream")
@mut('sync')
def update_pr(git_gecko,  # type: Repo
              git_wpt,  # type: Repo
              sync,  # type: UpstreamSync
              action,  # type: str
              merge_sha=None,  # type: Text
              base_sha=None,  # type: Text
              merged_by=None,  # type: str
              ):
    # type: (...) -> None
    """Update the sync status for a PR event on github

    :param action string: Either a PR action or a PR status
    :param merge_sha string: SHA of the new head if the PR merged or None if it didn't"""

    if action == "closed":
        if not merge_sha and sync.pr_status != "closed":
            env.bz.comment(sync.bug, "Upstream PR was closed without merging")
            sync.pr_status = "closed"
        else:
            sync.merge_sha = merge_sha
            if not sync.wpt_commits and base_sha:
                sync.set_wpt_base(base_sha)
            if sync.status not in ("complete", "wpt-merged"):
                env.bz.comment(sync.bug, "Upstream PR merged by %s" % merged_by)
                sync.finish("wpt-merged")
    elif action == "reopened" or action == "open":
        sync.pr_status = "open"
