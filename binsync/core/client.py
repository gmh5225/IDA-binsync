import logging
import pathlib
import os
import re
import subprocess
import time
from functools import wraps
from typing import Iterable, Optional

import filelock
import git
import git.exc

from binsync.data import User
from binsync.core.errors import ExternalUserCommitError, MetadataNotFoundError
from binsync.data.state import State, load_toml_from_file
from binsync.core.scheduler import Scheduler, Job, SchedSpeed
from binsync.core.cache import Cache


l = logging.getLogger(__name__)
BINSYNC_BRANCH_PREFIX = 'binsync'
BINSYNC_ROOT_BRANCH = f'{BINSYNC_BRANCH_PREFIX}/__root__'


class ConnectionWarnings:
    HASH_MISMATCH = 0


def atomic_git_action(f):
    """
    Assures that any function called with this decorator will execute in-order, atomically, on a single thread.
    This all assumes that the function you are passing is a member of the Client class, which will also have
    a scheduler. This also means that this can only be called after the scheduler is started. This also requires a
    Cache. Generally, just never call functions with this decorator until the Client is done initing.

    This function will also attempt to check the cache for requested data on the same thread the original call
    was made from. If not found, the atomic scheduling is done.

    @param f:   A Client object function
    @return:
    """
    @wraps(f)
    def _atomic_git_action(self: "Client", *args, **kwargs):
        no_cache = kwargs.get("no_cache", False)
        if not no_cache:
            # cache check
            cache_item = self._check_cache_(f, **kwargs)
            if cache_item is not None:
                return cache_item

        # non cache available, queue it up!
        priority = kwargs.get("priority", None) or SchedSpeed.SLOW
        ret_val = self.scheduler.schedule_and_wait_job(
            Job(f, self, *args, **kwargs),
            priority=priority
        )

        # set cache
        self._set_cache(f, ret_val, **kwargs)

        return ret_val

    return _atomic_git_action


class Client:
    def __init__(
        self,
        master_user: str,
        repo_root: str,
        binary_hash: bytes,
        remote: str = "origin",
        commit_interval: int = 10,
        init_repo: bool = False,
        remote_url: Optional[str] = None,
        ssh_agent_pid: Optional[int] = None,
        ssh_auth_sock: Optional[str] = None
    ):
        """
        The Client class is responsible for making the low-level Git operations for the BinSync environment.
        Things like committing, pulling, and pushing all happen on the client level. It also starts a thread
        for continuous pushing.

        :param master_user:         Username of the user that is initing the client (the master user)
        :param repo_root:           Path to the BinSync repo where the project will be stored
        :param binary_hash:         The hash, usually md5, of the binary the client is connected for
        :param remote:              The optional Git remote (usually origin)
        :param commit_interval:     The seconds between each commit in the worker thread
        :param init_repo:           Bool to decide initing for both remote and local repos
        :param remote_url:          Remote URL to a Git Repo which may be used for cloning or initing
        :param ssh_agent_pid:       SSH Agent PID
        :param ssh_auth_sock:       SSH Auth Socket
        """
        self.master_user = master_user
        self.repo_root = repo_root
        self.binary_hash = binary_hash
        self.remote = remote
        self.repo = None
        self.repo_lock = None

        # validate this username can exist
        if master_user.endswith('/') or '__root__' in master_user:
            raise Exception(f"Bad username: {master_user}")

        # ssh-agent info
        self.ssh_agent_pid = ssh_agent_pid  # type: int
        self.ssh_auth_sock = ssh_auth_sock  # type: str
        self.connection_warnings = []

        # job scheduler
        self.cache = Cache()
        self.scheduler = Scheduler()

        # create, init, and checkout Git repo
        self.repo = self._get_or_init_binsync_repo(remote_url, init_repo)
        self.scheduler.start_worker_thread()
        self._get_or_init_user_branch()

        # timestamps
        self._commit_interval = commit_interval
        self._last_push_ts = None  # type: Optional[float]
        self.last_push_attempt_ts = None  # type: Optional[float]
        self._last_pull_ts = None  # type: Optional[float]
        self.last_pull_attempt_ts = None  # type: Optional[float]
        self._last_commit_ts = None # type: Optional[float]

        self.active_remote = True

    def __del__(self):
        if self.repo_lock is not None:
            self.repo_lock.release()

    #
    # Initializers
    #

    def _get_or_init_user_branch(self):
        """
        Creates a user branch if the user is new, otherwise it gets the branch of the same
        name as the user

        @return:
        """
        try:
            branch = next(o for o in self.repo.branches if o.name.endswith(self.user_branch_name))
        except StopIteration:
            branch = self.repo.create_head(self.user_branch_name, BINSYNC_ROOT_BRANCH)
        else:
            if branch.is_remote():
                branch = self.repo.create_head(self.user_branch_name)
        branch.checkout()

    def _get_or_init_binsync_repo(self, remote_url, init_repo):
        """
        Gets the BinSync repo from either a local or remote git location and then sets up the repo as well
        as checks that the repo is the right repo for the current binary.

        When getting the repo there are four scenarios:
        1. The user fills out the remote_url and does not try to init it. In this case we should clone down
           the repo and assure that the head is remote/binsync/__root__
        2. The user fills out the remote_url and want to init it. In this case, we should clone it down to the repo
           root specified in the path. If there is no path, just place it locally then try to set it up with a hash
        3. The user fills out only the path of the git repo (no remote), then we just need to get that local git
           repo and assure it is a BinSync repo
        4. Last case is what happens if a user wants to init a local folder that is not a Git folder. In this case,
           they will be offline but will have a local git repo.

        @param remote_url:
        @param init_repo:
        @return:
        """
        if remote_url:
            # given a remove URL and no local folder, make it based on the URL name
            if not self.repo_root:
                self.repo_root = re.findall(r"/(.*)\.git", remote_url)[0]

            self.repo: git.Repo = self.clone(remote_url, no_head_check=init_repo)

            if init_repo:
                if any(b.name == BINSYNC_ROOT_BRANCH for b in self.repo.branches):
                    raise Exception("Can't init this remote repo since a BinSync root already exists")

                self._setup_repo()
        else:
            try:
                self.repo = git.Repo(self.repo_root)

                # update view of remote branches (which may be new)
                self._localize_remote_branches()

                if init_repo:
                    raise Exception("Could not initialize repository - it already exists!")
                if not any(b.name == BINSYNC_ROOT_BRANCH for b in self.repo.branches):
                    raise Exception(f"This is not a BinSync repo - it must have a {BINSYNC_ROOT_BRANCH} branch.")
            except (git.NoSuchPathError, git.InvalidGitRepositoryError):
                if init_repo:
                    # case 3
                    self.repo = git.Repo.init(self.repo_root)
                    self._setup_repo()
                else:
                    raise Exception(f"Failed to connect or create a BinSync repo")

        stored = self._get_stored_hash()
        if stored != self.binary_hash:
            self.connection_warnings.append(ConnectionWarnings.HASH_MISMATCH)

        assert not self.repo.bare, "it should not be a bare repo"

        self.repo_lock = filelock.FileLock(self.repo_root + "/.git/binsync.lock")
        try:
            self.repo_lock.acquire(timeout=0)
        except filelock.Timeout as e:
            raise Exception("Can only have one binsync client touching a local repository at once.\n"
                            "If the previous client crashed, you need to delete " + self.repo_root +
                            "/.git/binsync.lock") from e

        return self.repo

    def _setup_repo(self):
        """
        For use in initializing folder that is not yet a Git repo.

        @return:
        """
        with open(os.path.join(self.repo_root, ".gitignore"), "w") as f:
            f.write(".git/*\n")
        with open(os.path.join(self.repo_root, "binary_hash"), "w") as f:
            f.write(self.binary_hash)
        self.repo.index.add([".gitignore", "binary_hash"])
        self.repo.index.commit("Root commit")
        self.repo.create_head(BINSYNC_ROOT_BRANCH)

    #
    # Public Properties
    #

    @property
    def last_push_ts(self):
        return self._last_push_ts

    @property
    def last_pull_ts(self):
        return self._last_pull_ts

    @property
    def last_commit_ts(self):
        return self._last_commit_ts

    @property
    def user_branch_name(self):
        return f"{BINSYNC_BRANCH_PREFIX}/{self.master_user}"

    #
    # Atomic Public API
    #

    @atomic_git_action
    def commit_state(self, state, msg="Generic Change", priority=None):
        if self.master_user != state.user:
            raise ExternalUserCommitError(f"User {self.master_user} is not allowed to commit to user {state.user}")

        self._checkout_to_master_user()
        master_user_branch = next(o for o in self.repo.branches if o.name == self.user_branch_name)
        index = self.repo.index

        # dump the state
        state.dump(index)

        # commit changes
        self.repo.index.add([os.path.join(state.user, "*")])

        if not self.repo.index.diff("HEAD"):
            return

        # commit if there is any difference
        try:
            commit = index.commit(msg)
        except Exception as e:
            l.warning(f"Internal Git Commit Error: {e}")
            return

        self._last_commit_ts = time.time()
        master_user_branch.commit = commit
        state._dirty = False

    @atomic_git_action
    def users(self, priority=None, no_cache=False) -> Iterable[User]:
        repo = self.repo
        users = list()
        for ref in self._get_best_refs(repo).values():
            try:
                metadata = load_toml_from_file(ref.commit.tree, "metadata.toml", client=self)
                user = User.from_metadata(metadata)
                users.append(user)
            except Exception as e:
                l.debug(f"Unable to load user {e}")
                continue

        return users

    @atomic_git_action
    def get_state(self, user=None, version=None, priority=None, no_cache=False):
        if user is None:
            user = self.master_user

        repo = self.repo
        state = None
        try:
            state = State.parse(
                self._get_tree(user, repo),
                version=version,
                client=self
            )
        except MetadataNotFoundError:
            if user == self.master_user:
                state = State(self.master_user, client=self)

        return state

    @atomic_git_action
    def pull(self, priority=SchedSpeed.AVERAGE):
        """
        Pull changes from the remote side.

        :return:    None
        """
        self.last_pull_attempt_ts = time.time()
        try:
            env = self.ssh_agent_env()
        except Exception:
            return

        with self.repo.git.custom_environment(**env):
            # dangerous remote operations happen here
            try:
                self._localize_remote_branches()
                self.repo.git.checkout(BINSYNC_ROOT_BRANCH)
                self.repo.git.fetch("--all")
                self.repo.git.pull("--all")
                self._last_pull_ts = time.time()
            except Exception as e:
                l.debug(f"Pull exception {e}")

        # preform a merge on each branch
        for branch in self.repo.branches:
            if "HEAD" in branch.name:
                continue

            self.repo.git.checkout(branch)
            try:
                self.repo.git.merge()
            except Exception as e:
                l.debug(f"Failed to merge on {branch} with {e}")

        self._update_cache()

    @atomic_git_action
    def push(self, print_error=False, priority=SchedSpeed.AVERAGE):
        """
        Push local changes to the remote side.

        :return:    None
        """
        self.last_push_attempt_ts = time.time()
        self._checkout_to_master_user()
        try:
            env = self.ssh_agent_env()
            with self.repo.git.custom_environment(**env):
                self.repo.remotes[self.remote].push(BINSYNC_ROOT_BRANCH)
                self.repo.remotes[self.remote].push(self.user_branch_name)
            self._last_push_ts = time.time()
            l.debug("Push completed successfully at %s", self._last_push_ts)
        except git.exc.GitCommandError as ex:
            l.debug(f"Failed to push b/c {ex}")

    @property
    @atomic_git_action
    def has_remote(self, priority=SchedSpeed.FAST):
        """
        If there is a remote configured for our local repo.

        :return:    True if there is a remote, False otherwise.
        """

        if self.remote and any(r.name == self.remote for r in self.repo.remotes):
            return self.active_remote

        return False

    #
    # Non-Atomic Public API
    #

    def update(self, commit_msg="Generic Change"):
        """
        Update both the local and remote repo knowledge of files through pushes/pulls and commits
        in the case of dirty files.
        """
        # attempt to commit dirty files in a update phase
        master_state = self.get_state(user=self.master_user, no_cache=True)
        if master_state and master_state.dirty:
            self.commit_state(master_state, msg=commit_msg)

        # do a pull if there is a remote repo connected
        if self.has_remote:
            self.pull()

        if self.has_remote:
            self.push()

    #
    # Git Updates
    #

    def _localize_remote_branches(self):
        """
        Looks up all the remote refrences on the server and attempts to make them a tracked local
        branch.

        @return:
        """


        # get all remote branches
        try:
            remote_branches = self.repo.remote().refs
        except ValueError:
            return

        # track any remote we are not already tracking
        local_branches = set(b.name for b in self.repo.branches)
        for branch in remote_branches:
            # exclude head commit
            if "HEAD" in branch.name:
                continue

            # attempt to localize the remote name
            try:
                local_name = re.findall(f"({self.remote}/)(.*)", branch.name)[0][1]
            except IndexError:
                print("INDEX ERROR")
                continue

            # never try to track things already tracked
            if local_name in local_branches:
                continue

            try:
                self.repo.git.checkout('--track', branch.name)
            except git.GitCommandError as e:
                continue



    def ssh_agent_env(self):
        if self.ssh_agent_pid is not None and self.ssh_auth_sock is not None:
            env = {
                'SSH_AGENT_PID': str(self.ssh_agent_pid),
                'SSH_AUTH_SOCK': str(self.ssh_auth_sock),
            }
        else:
            env = { }
        return env

    def clone(self, remote_url, no_head_check=False):
        """
        Checkout from a remote_url to a local path specified by self.local_root.

        :param str remote_url:  The URL of the Git remote.
        :return:                None
        """

        env = self.ssh_agent_env()
        repo = git.Repo.clone_from(remote_url, self.repo_root, env=env)

        if no_head_check:
            return repo

        try:
            repo.create_head(BINSYNC_ROOT_BRANCH, f'{self.remote}/{BINSYNC_ROOT_BRANCH}')
        except git.BadName:
            raise Exception(f"This is not a binsync repo - it must have a {BINSYNC_ROOT_BRANCH} branch")

        return repo

    def _checkout_to_master_user(self):
        """
        Ensure the repo is in the proper branch for current user.

        :return: bool
        """
        self.repo.git.checkout(self.user_branch_name)

    @staticmethod
    def discover_ssh_agent(ssh_agent_cmd):
        proc = subprocess.Popen(ssh_agent_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()

        stdout = stdout.decode("utf-8")
        stderr = stderr.decode("utf-8")

        if proc.returncode != 0 or stderr:
            raise RuntimeError("Failed to discover SSH agent by running command %s.\n"
                               "Return code: %d.\n"
                               "stderr: %s" % (
                ssh_agent_cmd,
                proc.returncode,
                stderr,
            ))

        # parse output
        m = re.search(r"Found ssh-agent at (\d+)", stdout)
        if m is None:
            print("Failed to find 'Found ssh-agent at'")
            m = re.search(r"SSH_AGENT_PID=(\d+);", stdout)
            if m is None:
                print("Failed to find SSH_AGENT_PID")
                return None, None
            print("Found SSH_AGENT_PID")
            ssh_agent_pid = int(m.group(1))
            m = re.search("SSH_AUTH_SOCK=(.*?);", stdout)
            if m is None:
                print("Failed to find SSH_AUTH_SOCK")
                return None, None
            print("Found SSH_AGENT_SOCK")
            ssh_agent_sock = m.group(1)
        else :
            print("Found ssh-agent at")
            ssh_agent_pid = int(m.group(1))
            m = re.search(r"Found ssh-agent socket at ([^\s]+)", stdout)
            if m is None:
                print("Failed to find 'Found ssh-agent socket at'")
                return None, None
            print("Found ssh-agent socket at")
            ssh_agent_sock = m.group(1)

        return ssh_agent_pid, ssh_agent_sock

    def close(self):
        self.repo.close()
        del self.repo

    def _get_best_refs(self, repo):
        candidates = {}
        for ref in repo.refs:  # type: git.Reference
            if f'{BINSYNC_BRANCH_PREFIX}/' not in ref.name:
                continue

            branch_name = ref.name.split("/")[-1]
            if branch_name in candidates:
                # if the candidate exists, and the new one is not remote, don't replace it
                if not ref.is_remote() or ref.remote_name != self.remote:
                    continue

            candidates[branch_name] = ref
        return candidates

    def _get_stored_hash(self):
        branch = [ref for ref in self.repo.refs if ref.name.endswith(BINSYNC_ROOT_BRANCH)][0]
        return branch.commit.tree["binary_hash"].data_stream.read().decode().strip("\n")

    def list_files_in_tree(self, base_tree: git.Tree):
        """
        Lists all the files in a repo at a given tree

        :param commit: A gitpython Tree object
        """
        file_list = []
        stack = [base_tree]
        while len(stack) > 0:
            tree = stack.pop()
            # enumerate blobs (files) at this level
            for b in tree.blobs:
                file_list.append(b.path)
            for subtree in tree.trees:
                stack.append(subtree)

        return file_list

    def add_data(self, index: git.IndexFile, path: str, data: bytes):
        """
        Adds physical files to the database.

        WARNING: this function touches physical files in the Git Repo which can result in a race
        condition to modify a file while it is also being pushed. ONLY CALL THIS FUNCTION INSIDE
        A COMMIT_LOCK.

        @param index:
        @param path:
        @param data:
        @return:
        """
        fullpath = os.path.join(os.path.dirname(index.repo.git_dir), path)
        pathlib.Path(fullpath).parent.mkdir(parents=True, exist_ok=True)
        with open(fullpath, 'wb') as fp:
            fp.write(data)
        index.add([fullpath])

    def remove_data(self, index: git.IndexFile, path: str):
        fullpath = os.path.join(os.path.dirname(index.repo.git_dir), path)
        pathlib.Path(fullpath).parent.mkdir(parents=True, exist_ok=True)
        index.remove([fullpath], working_tree=True)

    def load_file_from_tree(self, tree: git.Tree, filename):
        return tree[filename].data_stream.read().decode()

    def _get_tree(self, user, repo: git.Repo):
        options = [ref for ref in repo.refs if ref.name.endswith(f"{BINSYNC_BRANCH_PREFIX}/{user}")]
        if not options:
            raise ValueError(f'No such user "{user}" found in repository')

        # find the latest commit for the specified user!
        best = max(options, key=lambda ref: ref.commit.authored_date)
        bct = best.commit.tree

        return bct

    #
    # Caching Functions
    #

    def _get_commits_for_users(self, repo: git.Repo):
        ref_dict = self._get_best_refs(repo)

        # ignore the _root_ branch
        if BINSYNC_ROOT_BRANCH in ref_dict:
            del ref_dict[BINSYNC_ROOT_BRANCH]

        commit_dict = {
            branch_name: ref.commit.hexsha for branch_name, ref in ref_dict.items()
        }
        return commit_dict

    def _check_cache_(self, f, **kwargs):
        if f.__qualname__ == self.get_state.__qualname__:
            cache_func = self.cache.get_state
            args = []
            if kwargs.get("user", None) is None:
                kwargs["user"] = self.master_user

        elif f.__qualname__ == self.users.__qualname__:
            cache_func = self.cache.users
            args = []
        else:
            return None

        item = cache_func(*args, **kwargs)

        return item

    def _set_cache(self, f, ret_value, **kwargs):
        if f.__qualname__ == self.get_state.__qualname__:
            set_func = self.cache.set_state
            args = []
            if kwargs.get("user", None) is None:
                kwargs["user"] = self.master_user
        elif f.__qualname__ == self.users.__qualname__:
            set_func = self.cache.set_users
            args = []
        else:
            return None

        set_func(ret_value, *args, **kwargs)

    def _update_cache(self):
        l.debug(f"Updating cache commits for State Cache...")
        cache_dict = self._get_commits_for_users(git.Repo(self.repo_root))
        self.cache.update_state_cache_commits(cache_dict)

        cache_keys = [key for key in cache_dict.keys()]
        l.debug(f"Updating branches on Users Cache...")
        branch_set = set(cache_keys)
        self.cache.update_user_cache_branches(branch_set)


