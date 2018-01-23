import argparse as ap
import json
import multiprocessing
import os
import shlex
import shutil
import subprocess as sp
import sys


TESTBENCH_ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config(filename):
    """Load all information from the specified config file."""

    config_path = os.path.join(TESTBENCH_ROOT, filename)

    with open(config_path, 'r') as config_file:
        config_dict = json.loads(config_file.read())

    if not config_dict:
        sys.stderr.write("[Error] Empty config file.\n")
        sys.exit(1)

    return config_dict


def run_command(cmd):
    """Wrapper function to handle running system commands."""

    proc = sp.Popen(shlex.split(cmd), stdin=sp.PIPE, stdout=sp.PIPE,
                    stderr=sp.PIPE)

    err = proc.communicate()[1]

    if proc.returncode is not 0:
        sys.stderr.write("[ERROR] %s\n" % str(err))

    return proc.returncode


def clone_project(project, project_dir):
    """Clone a single project.

    Its version is specified by a version tag or a commit hash
    found in the config file.

    If a project already exists, we simply overwrite it.
    """

    # If the project folder already exists, remove it.
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir)

    # If there is no tag specified, we clone the master branch.
    # This presumes that a master branch exists.
    if 'tag' not in project:
        project['tag'] = 'master'

    # Check whether the project config contains a version tag or a commit hash.
    try:
        int(project['tag'], base=16)
        commit_hash = True
    except:
        commit_hash = False

    # If the 'tag' value is a version tag, we can use shallow cloning.
    # With a commit hash, we need to clone everything and then checkout
    # the specified commit.
    cmd = {
        'clone': 'git clone %s --depth 1 %s' % (project['url'], project_dir)}

    if commit_hash:
        cmd['checkout'] = 'git --git-dir=%s/.git --work-tree=%s checkout %s' \
                          % (project_dir, project_dir, project['tag'])
    else:
        cmd['clone'] += ' --branch %s --single-branch' % project['tag']

    # Clone project.
    sys.stderr.write("Checking out '%s'...\n" % project['name'])
    clone_failed = run_command(cmd['clone'])
    if clone_failed:
        return False

    # Checkout specified commit if needed.
    if 'checkout' in cmd:
        checkout_failed = run_command(cmd['checkout'])
        if checkout_failed:
            return False

    return True


def identify_build_system(project, project_dir):
    """Identifies the build system of a project.

    Used heuristics:
        - If there's a 'CMakeLists.txt' file at the project root: 'cmake'.
        - If there's an 'autogen.sh' script at the project root: run it.
        - If there's a 'configure' script at the project root: run it,
          then return 'makefile'.

    The actual build-log generation happens in main().
    """

    project_files = os.listdir(project_dir)
    if not project_files:
        sys.stderr.write("[ERROR] No files found in '%s'.\n\n" % project_dir)
        return None

    if 'CMakeLists.txt' in project_files:
        return 'cmake'

    if 'Makefile' in project_files:
        return 'makefile'

    if 'autogen.sh' in project_files:
        # Autogen needs to be executed in the project's root directory.
        os.chdir(project_dir)
        autogen_failed = run_command("sh autogen.sh")
        os.chdir(os.path.dirname(project_dir))
        if autogen_failed:
            return None

    # Need to re-list files, as autogen might have generated a config script.
    project_files = os.listdir(project_dir)

    if 'configure' in project_files:
        os.chdir(project_dir)
        configure_failed = run_command("./configure")
        os.chdir(os.path.dirname(project_dir))
        if configure_failed:
            return None
        return 'makefile'

    sys.stderr.write("[ERROR] Build system cannot be identified.\n\n")
    return None


def check_logged(projects_root):
    """Post-script cleanup.

    Removes any projects that have an empty build-log JSON file
    at the end of the script.
    """

    projects = os.listdir(projects_root)
    for project in projects:
        log = os.path.join(projects_root, project, 'compile_commands.json')
        if not os.path.getsize(log) > 0:
            shutil.rmtree(os.path.join(projects_root, project))
    return os.listdir(projects_root)


def main():
    parser = ap.ArgumentParser(description="Build-log generator.\n" +
                               "\nClones projects and generates their" +
                               "build-logs into a JSON file.",
                               formatter_class=ap.RawTextHelpFormatter)
    parser.add_argument("--config", metavar="FILE",
                        default='test_config.json',
                        help="JSON file holding a list of projects")
    args = parser.parse_args()

    # Check if CodeChecker binary is in $PATH.
    try:
        run_command("CodeChecker version")
    except OSError as oerr:
        sys.stderr.write(
            "[ERROR] CodeChecker is not available as a command.\n")
        sys.exit(1)

    # Load configuration dictionary containing all project information.
    config_path = os.path.join(TESTBENCH_ROOT, args.config)
    sys.stderr.write("\nUsing configuration file '%s'.\n" % config_path)
    config = load_config(config_path)
    sys.stderr.write("Number of projects: %d.\n\n" % len(config['projects']))

    # Check if 'projects' folder exists. Create it if needed.
    projects_root = os.path.join(TESTBENCH_ROOT, 'projects')
    if not os.path.isdir(projects_root):
        os.mkdir(projects_root)

    for project in config['projects']:
        # Path to the root of the currently analyzed project:
        project_dir = os.path.join(projects_root, project['name'])

        # Clone projects (correct version / commit).
        clone_success = clone_project(project, project_dir)
        if not clone_success:
            shutil.rmtree(project_dir)
            continue

        # Identify build system (CMake / autotools)
        # + run configure script if needed.
        build_sys = identify_build_system(project, project_dir)
        if not build_sys:
            shutil.rmtree(project_dir)
            continue

        if build_sys == 'cmake':
            # Generate 'compile_commands.json' using CMake.
            cmd = "cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B%s -H%s" \
                  % (project_dir, project_dir)
            cmake_failed = run_command(cmd)
            if cmake_failed:
                shutil.rmtree(project_dir)
                continue
            sys.stderr.write("Build log generated successfully.\n\n")
            continue

        if build_sys == 'makefile':
            # Generate 'compile_commands.json' using CodeChecker.
            json_path = os.path.join(project_dir, "compile_commands.json")
            cmd = "CodeChecker log -b 'make -C%s -j%d' -o %s" \
                  % (project_dir, multiprocessing.cpu_count(), json_path)
            cc_failed = run_command(cmd)
            if cc_failed:
                shutil.rmtree(project_dir)
                continue
            sys.stderr.write("Build log generated successfully.\n\n")

    logged_projects = check_logged(projects_root)
    sys.stderr.write("\n# of successfully logged projects: %d / %d\n\n"
                     % (len(logged_projects), len(config['projects'])))


if __name__ == '__main__':
    main()
