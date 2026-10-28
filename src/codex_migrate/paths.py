"""Explicit cross-platform path mapping; local references never trigger discovery."""
from __future__ import annotations

import hashlib
import ntpath
import os
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit

from .errors import MigrationError


def _windows(value: str) -> bool:
    return bool(re.match(r'^[A-Za-z]:[/\\]', value)) or value.startswith(('\\\\', '//'))


def _clean(value: str) -> tuple[str, str]:
    value = value.replace('\\', '/') if _windows(value) else value
    if value.startswith('//?/UNC/'):
        value = '//' + value[8:]
    elif value.startswith('//?/'):
        value = value[4:]
    if _windows(value):
        return 'windows', ntpath.normpath(value).replace('\\', '/')
    if value.startswith('/'):
        return 'posix', posixpath.normpath(value)
    raise MigrationError('Mapping requires an absolute path', code='invalid_path')


def _key(value: str) -> tuple[str, str]:
    flavor, normalized = _clean(value)
    return flavor, normalized.casefold() if flavor == 'windows' else normalized


def _within(value: str, root: str) -> bool:
    return value == root or value.startswith(root.rstrip('/') + '/')


def _local(value: str) -> bool:
    return _windows(value) == (os.name == 'nt')


class PathMapper:
    """Map declared source roots to final roots without creating any destination.

    ``validation_mappings`` maps a final root to its extracted stage root solely
    for existence, containment and digest checks. Repaired links always emit
    final paths. Expected SHA-256 keys may be normalized old or final paths;
    conflicting declarations are reported as ambiguous.
    """
    def __init__(self, mappings: list[tuple[str, str]], *, expected_hashes: dict[str, str] | None = None,
                 validation_mappings: list[tuple[str, str]] | None = None):
        self.roots = []
        for old, new in mappings:
            source_flavor, source = _clean(old)
            _, target = _clean(new)
            self.roots.append((source_flavor, source, target))
        self.roots.sort(key=lambda entry: len(entry[1]), reverse=True)
        self.expected_hashes = {_key(path): digest.lower() for path, digest in (expected_hashes or {}).items()}
        self._validation = None
        if validation_mappings:
            for final, stage in validation_mappings:
                flavor, normalized = _key(final)
                if not any(flavor == _key(target)[0] and _within(normalized, _key(target)[1]) for _, _, target in self.roots):
                    raise MigrationError('Validation root lies outside planned final roots', code='path_mapping')
            self._validation = PathMapper(validation_mappings)

    def _validation_target(self, mapped: str, root: str) -> tuple[str, str] | None:
        if self._validation is None:
            return mapped, root
        matches = self._validation._candidates(mapped)
        if len(matches) == 1:
            return matches[0]
        return None

    def _candidates(self, value: str) -> list[tuple[str, str]]:
        try:
            flavor, normalized = _clean(value)
        except MigrationError:
            return []
        value_key = normalized.casefold() if flavor == 'windows' else normalized
        matches = []
        longest = -1
        for source_flavor, source, target in self.roots:
            source_key = source.casefold() if source_flavor == 'windows' else source
            if flavor != source_flavor or not _within(value_key, source_key):
                continue
            if len(source) < longest:
                continue
            if len(source) > longest:
                matches.clear()
                longest = len(source)
            suffix = normalized[len(source.rstrip('/')):].lstrip('/')
            mapped = target.rstrip('/') + ('/' + suffix if suffix else '')
            matches.append((mapped or '/', target))
        return list(dict.fromkeys(matches))

    def map_path(self, value: str, required: bool = False) -> str | None:
        matches = self._candidates(value) if isinstance(value, str) else []
        if len(matches) == 1:
            mapped, root = matches[0]
            if _local(mapped):
                try:
                    Path(mapped).resolve().relative_to(Path(root).resolve())
                except (ValueError, OSError, RuntimeError):
                    if required:
                        raise MigrationError('Structural path escapes selected root', code='path_mapping')
                    return None
            return mapped
        if required:
            reason = 'ambiguous' if matches else 'unmapped'
            raise MigrationError(f'{reason} required structural path', code='path_mapping')
        return None

    def resolve_link(self, value: str) -> dict:
        record = {'status': 'missing', 'old': value, 'new': None, 'path': None}
        if not isinstance(value, str) or not value or '\x00' in value:
            return {**record, 'status': 'unsupported-syntax', 'reason': 'invalid destination'}
        destination = re.sub(r'\\([ ()\[\]<>])', r'\1', value)
        suffix = ''
        if '#' in destination:
            destination, fragment = destination.split('#', 1)
            suffix = '#' + fragment
        line_suffix = re.search(r'(:\d+(?::\d+)?)$', destination)
        if line_suffix:
            suffix = line_suffix.group() + suffix
            destination = destination[:line_suffix.start()]
        if destination.lower().startswith('file:'):
            parsed = urlsplit(destination)
            if parsed.query or parsed.username or parsed.password:
                return {**record, 'status': 'unsupported-syntax', 'reason': 'unsupported file URL'}
            if not parsed.path:
                return {**record, 'status': 'unsupported-syntax', 'reason': 'empty file URL'}
            destination = ('//' + parsed.netloc if parsed.netloc and parsed.netloc != 'localhost' else '') + unquote(parsed.path)
            if re.match(r'^/[A-Za-z]:/', destination):
                destination = destination[1:]
        elif re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', destination) and not _windows(destination):
            return {**record, 'status': 'external-url'}
        else:
            destination = unquote(destination)
        matches = self._candidates(destination)
        absolute = _windows(destination) or destination.startswith('/')
        if not absolute:
            # Relative links may resolve only beneath explicitly selected roots.
            relative = destination.replace('\\', '/')
            if relative.startswith('../') or '/../' in relative or relative in ('..', '.'):
                return {**record, 'reason': 'relative traversal rejected'}
            matches = []
            for _, source, target in self.roots:
                candidate = target.rstrip('/') + '/' + relative
                validation = self._validation_target(candidate, target)
                if validation and _local(validation[0]) and Path(validation[0]).exists():
                    matches.append((candidate, target))
            matches = list(dict.fromkeys(matches))
        if len(matches) > 1:
            return {**record, 'status': 'ambiguous', 'reason': 'multiple explicit candidates'}
        if not matches:
            return {**record, 'reason': 'no mapped target'}
        mapped, root = matches[0]
        record['path'] = mapped
        validation = self._validation_target(mapped, root)
        if validation is None:
            return {**record, 'reason': 'mapped target has no unique staged validation root'}
        validation_path, validation_root = validation
        if not _local(validation_path):
            return {**record, 'reason': 'validation platform is not local'}
        # Check final containment independently even though the final file may
        # not exist yet. Existing final symlinks cannot redirect publication.
        if _local(mapped):
            try:
                Path(mapped).resolve().relative_to(Path(root).resolve())
            except (ValueError, OSError, RuntimeError):
                return {**record, 'reason': 'final target escapes selected root'}
        path, selected = Path(validation_path), Path(validation_root)
        try:
            path.resolve().relative_to(selected.resolve())
        except (ValueError, OSError, RuntimeError):
            return {**record, 'reason': 'target escapes selected root'}
        if not path.exists() or not (path.is_file() or path.is_dir()):
            return {**record, 'reason': 'target missing or not a regular file/directory'}
        expected_values = {self.expected_hashes[_key(mapped)]} if _key(mapped) in self.expected_hashes else set()
        if absolute and _key(destination) in self.expected_hashes:
            expected_values.add(self.expected_hashes[_key(destination)])
        if not absolute:
            expected_values.update({self.expected_hashes[_key(source.rstrip('/') + '/' + destination.replace('\\', '/'))]
                               for _, source, target in self.roots if target == root
                               and _key(source.rstrip('/') + '/' + destination.replace('\\', '/')) in self.expected_hashes})
        if len(expected_values) > 1:
            return {**record, 'status': 'ambiguous', 'reason': 'conflicting source/final manifests'}
        expected = next(iter(expected_values), None)
        status = 'user-mapped-unverified'
        if expected and path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                return {**record, 'reason': 'target hash differs from manifest'}
            status = 'verified'
        return {**record, 'status': status, 'new': mapped + suffix, 'reason': 'manifest hash verified' if status == 'verified' else 'user mapped; original content identity not verified'}
