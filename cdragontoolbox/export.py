import os
import re
import shutil
import subprocess
import time
import logging
from typing import Optional, Generator
from .storage import Version, Storage, PatchVersion
from .wad import Wad

logger = logging.getLogger(__name__)


def paths_to_tree(paths):
    """Reduce an iterable of paths into nested mappings

    For leafs, value if set to None.
    There must not be paths empty parts, leading or trailing slashes.

    For instance: ['a/x/1', 'a/2']
    Is reduced to: {'a': {'x': {'1': None}, '2': None}}
    """

    tree = {}
    for path in paths:
        *parents, leaf = path.split('/')
        subtree = tree
        for parent in parents:
            subtree = subtree.setdefault(parent, {})
        subtree[leaf] = None
    return tree

def reduce_common_trees(parts, tree1, tree2, excludes):
    """Recursive method for reducing paths"""
    if tree1 is None or (tree1 == tree2 and excludes is None):
        # leaf or common, non-excluded subtree
        yield '/'.join(parts)
        return
    for name in tree1:
        # non-common subtree, compare each subtree
        # tree2[name] must exist, since tree1 must be a subtree of tree2
        yield from reduce_common_trees(parts + [name], tree1[name], tree2[name], None if excludes is None else excludes.get(name))

def reduce_common_paths(paths1, paths2, excludes):
    """Compare paths lists and return the most common subpaths

    Reduce directories in paths1 that are the same in paths2 so that the
    returned list of paths are common in paths1 and paths2.
    All paths in paths1 must exist in paths2.

    If paths lists are identical, return a list of root's subdirs.
    """

    tree1 = paths_to_tree(paths1)
    tree2 = paths_to_tree(paths2)
    tree_excludes = paths_to_tree(excludes)
    ret = list(reduce_common_trees([], tree1, tree2, tree_excludes))
    if len(ret) == 1 and ret[0] == '':
        # trees are identical
        return list(tree1)
    return ret


def interruptible_subprocess_run(args, check=False, **kwargs):
    # on Windows, ^C does not work as expected and
    # we have to complicate things a bit...
    proc = subprocess.Popen(args, **kwargs)
    try:
        while proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    ret = subprocess.CompletedProcess(args, proc.returncode)
    if check:
        ret.check_returncode()
    return ret

# requires the following variables: $patch, $prev
# must run from export directory
_update_symlinks_script = """
set -e
while read f; do
  destf="$PWD/$patch/$f"
  if [ -h "$destf" ]; then
    continue  # already set
  elif [ -e "$destf" ]; then
    echo >&2 "symlink target already exists: $destf"
    false
  else
    echo "create $destf"
    mkdir -p "$(dirname "$destf")"
    ln -rs "$(readlink -f "$PWD/$prev/$f")" "$destf"
  fi
done < "$PWD/$patch.links.txt"
"""



class Exporter:
    """Handle export of multiple patchs in the same directory"""

    def __init__(self, storage: Storage, output: str, stored=True):
        self.output = output

        versions = set()
        for path in os.listdir(output):
            if not os.path.isdir(os.path.join(output, path)):
                continue
            try:
                version = Version(path)
            except (ValueError, TypeError):
                continue
            versions.add(version)

        if not versions:
            raise ValueError("no version directory found")

        patches = []
        for patch in PatchVersion.versions(storage, stored=stored):
            if patch.version in versions:
                patches.append(patch)
                versions.remove(patch.version)
                if not versions:
                    break
        else:
            raise ValueError("versions not found: %r" % versions)

        self.exporters = []
        for patch, previous_patch in zip(patches, patches[1:] + [None]):
            patch_output = os.path.join(output, str(patch.version))
            self.exporters.append(PatchExporter(patch_output, patch, previous_patch))

    def update(self):
        """Update all patches of the directory"""

        for exporter in self.exporters:
            exporter.export(overwrite=False)
            exporter.write_links()

    def upload(self, target):
        """Synchronize all patches to a remote storage"""

        # sync in reverse order, because of symlinks dependencies
        for exporter in self.exporters[::-1]:
            exporter.upload(target)

    def create_symlinks(self):
        """Create symlinks for all patches"""

        # create in reverse order, because of chained symlinks
        for exporter in self.exporters[::-1]:
            exporter.create_symlinks()


class PatchExporter:
    """Handle export of patch files to a directory"""

    def __init__(self, output: str, patch: PatchVersion, previous_patch: Optional[PatchVersion]):
        self.storage = patch.storage
        self.output = os.path.normpath(output)
        self.patch = patch
        self.previous_patch = previous_patch
        # list of export path to link from the previous patch, set in export()
        self.previous_links = None

    def export(self, overwrite=True):
        """Export modified files to the output directory, set previous_links

        Files that have changed from the previous patch are copied to the
        output directory.
        Files that didn't changed are added to self.previous_links. It's
        content is reduced so that identical directories result into a single
        link entry.
        """

        if self.previous_patch:
            logger.info("exporting patch %s based on patch %s", self.patch.version, self.previous_patch.version)
        else:
            logger.info("exporting patch %s based on (full)", self.patch.version)

        #XXX for now, only export files from league_client as lol_game_client is not well known yet
        patch_solutions = [sv for sv in self.patch.solutions(latest=True) if sv.solution.name == 'league_client_sln']
        for sv in patch_solutions:
            sv.download(langs=True)

        if self.previous_patch:
            prev_patch_solutions = [sv for sv in self.previous_patch.solutions(latest=True) if sv.solution.name == 'league_client_sln']
            for sv in prev_patch_solutions:
                sv.download(langs=True)

        # iterate on project files, compare files with previous patch
        projects = {pv for sv in patch_solutions for pv in sv.projects(True)}
        # match projects with previous projects
        if self.previous_patch:
            prev_projects = {pv.project.name: pv for sv in prev_patch_solutions for pv in sv.projects(True)}
        else:
            prev_projects = {}

        # get stored files, to remove superfluous ones
        original_exported_files = set(self.exported_files())

        previous_links = []
        extracted_paths = []
        for pv, prev_pv in sorted((pv, prev_projects.get(pv.project.name)) for pv in projects):
            # get export paths from previous package
            prev_extract_paths = {}  # {export_path: extract_path}
            if prev_pv:
                for path in prev_pv.filepaths():
                    prev_extract_paths[self.to_export_path(path)] = path

            for extract_path in pv.filepaths():
                export_path = self.to_export_path(extract_path)
                prev_extract_path = prev_extract_paths.get(export_path)
                # package files are identical if their extract paths are the same

                if extract_path.endswith('.wad'):
                    # WAD file: link the whole archive or compare file by file using sha256
                    wad = self._open_wad(extract_path)

                    if extract_path == prev_extract_path:
                        logger.debug("unchanged WAD file: %s", extract_path)
                        previous_links += [wf.path for wf in wad.files]
                    else:
                        logger.debug("modified WAD file: %s", extract_path)
                        if prev_extract_path:
                            # compare to the previous WAD based on sha256 hashes
                            # note: no need to use _open_wad(), file paths are not used
                            prev_wad = Wad(self.storage.fspath(prev_extract_path), hashes={})
                            prev_sha256 = {wf.path_hash: wf.sha256 for wf in prev_wad.files}
                            wadfiles_to_extract = []
                            for wf in wad.files:
                                if wf.sha256 == prev_sha256.get(wf.path_hash):
                                    # same file, add a link
                                    previous_links.append(wf.path)
                                else:
                                    wadfiles_to_extract.append(wf)
                            # change the files from the wad so it only extract these
                            wad.files = wadfiles_to_extract
                        extracted_paths += [wf.path for wf in wad.files]

                        logger.info("exporting %d files from %s", len(wad.files), extract_path)
                        wad.extract(self.output, overwrite=overwrite)

                else:
                    # ignore description.json files
                    # They may also also be in WADs (slightly different
                    # though), which may result into files being both extracted
                    # and symlinked.
                    # Just use WAD ones, even if it leads to not having a
                    # description.json at all. These files are not needed
                    # anyway.
                    if extract_path.endswith('/description.json'):
                        continue

                    # normal file: link or copy
                    if extract_path == prev_extract_path:
                        logger.debug("unchanged file: %s", extract_path)
                        previous_links.append(export_path)
                    else:
                        logger.debug("modified file: %s", extract_path)
                        extracted_paths.append(export_path)
                        self.export_storage_file(extract_path, export_path, overwrite=overwrite)

        # remove extra files
        dirs_to_remove = set()
        for path in original_exported_files - set(extracted_paths):
            logger.info("remove extra file: %s", path)
            full_path = os.path.join(self.output, path)
            os.remove(full_path)
            dirs_to_remove.add(os.path.dirname(full_path))
        for path in dirs_to_remove:
            try:
                os.removedirs(path)
            except OSError:
                pass

        if self.previous_patch:
            # get all files from the previous patch to properly reduce the links
            previous_files = []
            for pv in prev_projects.values():
                for path in pv.filepaths():
                    if path.endswith('.wad'):
                        wad = self._open_wad(path)
                        previous_files += [wf.path for wf in wad.files]
                    else:
                        previous_files.append(self.to_export_path(path))

            # check for files both extracted and linked
            # should not happen except in case of duplicated file
            duplicates = set(previous_links) & set(extracted_paths)
            if len(duplicates):
                raise RuntimeError("duplicate files: %r" % duplicates)

            self.previous_links = reduce_common_paths(previous_links, previous_files, extracted_paths)

    def _open_wad(self, extract_path: str) -> Wad:
        """Open a WAD, guess extensions and resolve paths"""

        wad = Wad(self.storage.fspath(extract_path))
        wad.guess_extensions()
        # set directory for unknown paths depending on WAD path
        m = re.search(r'/(plugins/rcp-.+?)/[^/]*assets\.wad$', extract_path, re.I)
        unknown_path = 'unknown'
        if m is not None:
            # LCU client: plugins/<plugin-name>
            unknown_path = '%s/unknown' % m.group(1).lower()
        wad.set_unknown_paths(unknown_path)
        return wad

    def export_storage_file(self, storage_path, export_path, overwrite=True):
        output_path = os.path.join(self.output, export_path)
        if overwrite and os.path.exists(output_path):
            return
        logger.info("exporting %s", export_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copyfile(self.storage.fspath(storage_path), output_path)

    def exported_files(self) -> Generator[str, None, None]:
        """Generate a list of files on disk (even if not if patch files)

        Generate paths with forward slashes on all platforms.
        """

        sep = os.path.sep
        for root, dirs, files in os.walk(self.output):
            if files:
                base = os.path.relpath(root, self.output)
                if base == '.':
                    base = ''
                else:
                    if sep != '/':
                        base = base.replace(sep, '/')
                    base += '/'
                for name in files:
                    yield f"{base}{name}"

    def write_links(self, path=None):
        if self.previous_links is None:
            return
        if path is None:
            path = self.output + '.links.txt'
        with open(path, 'w', newline='\n') as f:
            for link in sorted(self.previous_links):
                print(link, file=f)

    def create_symlinks(self):
        if self.previous_links is None:
            return
        dst_output = self.output
        src_output = os.path.join(os.path.dirname(self.output), str(self.previous_patch.version))

        logger.info("creating symlinks for patch %s", self.patch.version)
        for link in self.previous_links:
            dst = os.path.join(dst_output, link)
            if os.path.exists(dst):
                if not os.path.islink(dst):
                    raise RuntimeError("symlink target already exists: %s" % dst)
                continue  # already set
            dst_dir = os.path.dirname(dst)
            os.makedirs(dst_dir, exist_ok=True)
            src = os.path.relpath(os.path.realpath(os.path.join(src_output, link)), dst_dir)
            logger.info("create symlink %s", dst)
            os.symlink(src, dst)

    @staticmethod
    def to_export_path(path):
        """Compute path to which export the file from storage path"""
        # projects/<p_name>/releases/<p_version>/files/<export_path>
        return path.split('/', 5)[5].lower()

    def upload(self, target):
        """Synchronize the patch to a remote storage

        Use rsync to synchronize the files, then update symlinks.
        """

        if ':' not in target:
            raise ValueError("cannot extract host from target")
        target_host, target_dir = target.rsplit(':', 1)
        output_dir = self.output
        # make sure to use only paths with forward slashes, even on Windows,
        # since they are also used by the remote target
        output_dir = self.output.replace(os.path.sep, '/')

        logger.info("synchronize %s to %s", self.patch, target)

        args = [
            'rsync', '--progress', '--delete', '-rtOJ', '--size-only',
            f"{output_dir}/", f"{target}/{self.patch.version}/",
        ]
        if self.previous_patch:
            args += ['--exclude-from', f"{output_dir}.links.txt"]
        interruptible_subprocess_run(args, check=True)

        if self.previous_patch:
            logger.info("copy %s.links.txt to %s", self.patch.version, target)
            subprocess.run(['scp', f"{output_dir}.links.txt", f"{target}/"], check=True)

            logger.info("update links for %s on %s", self.patch, target)
            interruptible_subprocess_run(['ssh', target_host, f"patch={self.patch.version}\nprev={self.previous_patch.version}\ncd {target_dir}\n" + _update_symlinks_script])

