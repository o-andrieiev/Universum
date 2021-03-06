import inspect
import json
import shutil
import sh

from typing import cast, Dict, List, Type, Union

from . import git_vcs, github_vcs, gerrit_vcs, perforce_vcs, local_vcs, base_vcs
from .. import artifact_collector
from ..api_support import ApiSupport
from ..project_directory import ProjectDirectory
from ..structure_handler import needs_structure
from ...lib import utils
from ...lib.gravity import Dependency
from ...lib.module_arguments import IncorrectParameterError
from ...lib.utils import make_block

__all__ = [
    "MainVcs",
    "PollVcs",
    "SubmitVcs"
]


def create_vcs(class_type: str = None) -> Type[ProjectDirectory]:
    driver_factory_class: Union[
        Dict[str, Type[base_vcs.BasePollVcs]],
        Dict[str, Type[base_vcs.BaseSubmitVcs]],
        Dict[str, Type[base_vcs.BaseDownloadVcs]]
    ]
    if class_type == "submit":
        driver_factory_class = {
            "none" : base_vcs.BaseSubmitVcs,
            "p4" : perforce_vcs.PerforceSubmitVcs,
            "git" : git_vcs.GitSubmitVcs,
            "gerrit" : gerrit_vcs.GerritSubmitVcs,
            "github" : git_vcs.GitSubmitVcs
        }
    elif class_type == "poll":
        driver_factory_class = {
            "none": base_vcs.BasePollVcs,
            "p4": perforce_vcs.PerforcePollVcs,
            "git": git_vcs.GitPollVcs,
            "gerrit": git_vcs.GitPollVcs,
            "github": git_vcs.GitPollVcs
        }
    else:
        driver_factory_class = {
            "none": local_vcs.LocalMainVcs,
            "p4": perforce_vcs.PerforceMainVcs,
            "git": git_vcs.GitMainVcs,
            "gerrit": gerrit_vcs.GerritMainVcs,
            "github": github_vcs.GithubMainVcs
        }

    vcs_types: List[str] = ["none", "p4", "git", "gerrit", "github"]

    @needs_structure
    class Vcs(ProjectDirectory):
        driver_factory: Dict[str, Dependency] = {vcs_type: Dependency(cls)
                                                       for vcs_type, cls in driver_factory_class.items()}

        @staticmethod
        def define_arguments(argument_parser):
            parser = argument_parser.get_or_create_group("Source files")

            parser.add_argument("--vcs-type", "-vt", dest="type",
                                choices=vcs_types, metavar="VCS_TYPE",
                                help="Select repository type to download sources from: Perforce ('p4'), "
                                     "Git ('git'), Gerrit ('gerrit'), GitHub ('github') or a local directory ('none'). "
                                     "Gerrit uses Git parameters. Each VCS type has its own settings.")

        def __init__(self, *args, **kwargs):
            super(Vcs, self).__init__(*args, **kwargs)

            if not getattr(self.settings, "type", None):
                text = inspect.cleandoc("""
                    The repository (VCS) type is not set.
                     
                    The repository type defines the version control system 
                    that is used for performing the requested action.
                    For example, Universum needs to get project source codes
                    for performing Continuous Integration (CI) builds.  

                    The following types are supported: {}.
                    
                    Each of these types requires supplying its own
                    configuration parameters. At the minimum, the following
                    parameters are required:
                      * "git", "github" and "gerrit" - GIT_REPO (-gr) and GIT_REFSPEC (-grs)
                      * "perforce"                   - P4PORT (-p4p), P4USER (-p4u) and P4PASSWD (-p4P)
                      * "none"                       - SOURCE_DIR (-fsd)
                      
                    Depending on the requested action, additional type-specific
                    parameters are required. For example, P4CLIENT (-p4c) is
                    required for CI builds with perforce.""").format(", ".join(vcs_types))
                raise IncorrectParameterError(text)

            try:
                self.driver = driver_factory[self.settings.type]()
            except AttributeError | KeyError:
                raise NotImplementedError()

        @make_block("Finalizing")
        def finalize(self):
            self.driver.finalize()

    return Vcs


PollVcs: Type[ProjectDirectory] = create_vcs("poll")
SubmitVcs: Type[ProjectDirectory] = create_vcs("submit")


class MainVcs(create_vcs()):  # type: ignore  # https://github.com/python/mypy/issues/2477
    artifacts_factory = Dependency(artifact_collector.ArtifactCollector)
    api_support_factory = Dependency(ApiSupport)

    @staticmethod
    def define_arguments(argument_parser):
        parser = argument_parser.get_or_create_group("Source files")

        parser.add_argument("--report-to-review", action="store_true", dest="report_to_review", default=False,
                            help="Perform test build for code review system (e.g. Gerrit or Swarm).")

    def __init__(self, *args, **kwargs):
        super(MainVcs, self).__init__(*args, **kwargs)
        self.artifacts = self.artifacts_factory()
        self.api_support = self.api_support_factory()

        if self.settings.report_to_review:
            self.code_review = self.driver.code_review()

    def is_latest_review_version(self):
        if self.settings.report_to_review:
            return self.code_review.is_latest_version()
        return True

    @make_block("Preparing repository")
    def prepare_repository(self):
        status_file = self.artifacts.create_text_file("REPOSITORY_STATE.txt")

        self.driver.prepare_repository()

        status_file.write(self.driver.get_repo_status())

        status_file.write("\nFile list:\n\n")
        status_file.write(utils.trim_and_convert_to_unicode(sh.ls("-lR", self.settings.project_root)) + "\n")
        status_file.close()

        file_diff = self.driver.calculate_file_diff()
        self.api_support.add_file_diff(json.dumps(file_diff, indent=4))

    def clean_sources_silently(self):
        try:
            shutil.rmtree(self.settings.project_root)
        except OSError:
            pass

    @make_block("Revert repository")
    def revert_repository(self):
        diff = self.driver.copy_cl_files_and_revert()
        return diff
