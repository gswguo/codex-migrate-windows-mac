"""Synthetic cases: prevent prefix guesses, unsafe links, and prose corruption."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from codex_migrate.paths import PathMapper
from codex_migrate.errors import MigrationError
from codex_migrate.links import repair_markdown, transform_item


class PathsLinksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / '项目 space'
        self.target.mkdir()
        self.file = self.target / '报告 (final).pdf'
        self.file.write_bytes(b'fictional report')
        self.mapper = PathMapper([('E:/work', str(self.target))])

    def test_windows_case_segments_longest_prefix_and_required_mapping(self):
        nested = self.root / 'nested'
        mapper = PathMapper([('E:/work', str(self.target)), ('e:/WORK/sub', str(nested))])
        self.assertEqual(mapper.map_path(r'e:\WORK\sub\文.txt'), (nested / '文.txt').as_posix())
        self.assertIsNone(mapper.map_path('E:/worker/file'))
        with self.assertRaises(MigrationError):
            mapper.map_path('Q:/unmapped', required=True)
        self.assertIsNone(mapper.map_path('E:/work/../../escape'))

    def test_structural_paths_cannot_escape_through_symlink(self):
        outside = self.root / 'external'
        outside.mkdir()
        (self.target / 'escape').symlink_to(outside, target_is_directory=True)
        self.assertIsNone(self.mapper.map_path('E:/work/escape/child'))
        with self.assertRaises(MigrationError):
            self.mapper.map_path('E:/work/escape/child', required=True)

    def test_unsupported_local_markdown_reports_without_guessing(self):
        text = '[bad](E:/work/unbalanced(foo.pdf)'
        updated, records = repair_markdown(text, self.mapper)
        self.assertEqual(updated, text)
        self.assertEqual(records[0]['status'], 'unsupported-syntax')
        self.assertEqual(self.mapper.resolve_link('file:')['status'], 'unsupported-syntax')

    def test_unc_extended_and_reverse_windows_mapping(self):
        mapper = PathMapper([(r'\\server\share\root', str(self.target)), ('/old/project', 'D:/New Project')])
        self.assertEqual(mapper.map_path(r'\\?\UNC\server\share\root\中文.txt'), (self.target / '中文.txt').as_posix())
        self.assertEqual(mapper.map_path('/old/project/folder/file.txt'), 'D:/New Project/folder/file.txt')
        self.assertIsNone(mapper.map_path('/old/project2/file'))

    def test_file_url_percent_encoding_suffix_and_hash(self):
        old = 'E:/work/报告 (final).pdf'
        mapper = PathMapper([('E:/work', str(self.target))], expected_hashes={old: hashlib.sha256(self.file.read_bytes()).hexdigest()})
        result = mapper.resolve_link('file:///E:/work/%E6%8A%A5%E5%91%8A%20(final).pdf#page=3')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['path'], self.file.as_posix())
        self.assertTrue(result['new'].endswith('#page=3'))
        wrong = PathMapper([('E:/work', str(self.target))], expected_hashes={old: '0' * 64})
        self.assertEqual(wrong.resolve_link(old)['status'], 'missing')

    def test_stage_validation_emits_final_paths_without_creating_final_root(self):
        final = self.root / 'not-created-final'
        digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        mapper = PathMapper([('E:/work', str(final))], validation_mappings=[(str(final), str(self.target))], expected_hashes={str(final / self.file.name): digest})
        result = mapper.resolve_link('E:/work/' + self.file.name)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['new'], (final / self.file.name).as_posix())
        self.assertEqual(result['path'], (final / self.file.name).as_posix())
        self.assertEqual(mapper.resolve_link(self.file.name)['status'], 'verified')
        self.assertFalse(final.exists())
        with self.assertRaises(MigrationError):
            PathMapper([('E:/work', str(final))], validation_mappings=[(str(self.root / 'unplanned'), str(self.target))])

    def test_stage_validation_rejects_stage_and_final_symlink_escape(self):
        final = self.root / 'final'
        final.mkdir()
        external = self.root / 'external'
        external.mkdir()
        (external / 'f').write_bytes(b'outside')
        (self.target / 'escape').symlink_to(external, target_is_directory=True)
        mapper = PathMapper([('E:/work', str(final))], validation_mappings=[(str(final), str(self.target))])
        self.assertEqual(mapper.resolve_link('E:/work/escape/f')['status'], 'missing')
        (final / 'escape').symlink_to(external, target_is_directory=True)
        (self.target / 'safe').write_bytes(b'fine')
        (final / 'safe').symlink_to(external / 'f')
        self.assertEqual(mapper.resolve_link('E:/work/safe')['status'], 'missing')

    def test_relative_manifest_hash_is_checked_against_explicit_source_root(self):
        mapper = PathMapper([('E:/work', str(self.target))], expected_hashes={'E:/work/' + self.file.name: '0' * 64})
        result = mapper.resolve_link(self.file.name)
        self.assertEqual(result['status'], 'missing')
        self.assertIn('hash', result['reason'])

    def test_relative_ambiguity_missing_and_symlink_escape(self):
        other = self.root / 'other'
        other.mkdir()
        (other / self.file.name).write_bytes(b'another')
        mapper = PathMapper([('E:/work', str(self.target)), ('F:/work', str(other))])
        self.assertEqual(mapper.resolve_link(self.file.name)['status'], 'ambiguous')
        self.assertEqual(mapper.resolve_link('E:/work/missing.txt')['status'], 'missing')
        outside = self.root / 'outside.txt'
        outside.write_text('private')
        (self.target / 'escape.txt').symlink_to(outside)
        self.assertEqual(mapper.resolve_link('E:/work/escape.txt')['status'], 'missing')
        self.assertEqual(mapper.resolve_link('https://example.invalid/file')['status'], 'external-url')

    def test_duplicate_roots_are_ambiguous_not_first_wins(self):
        mapper = PathMapper([('E:/work', str(self.target)), ('e:/WORK', str(self.root))])
        with self.assertRaises(MigrationError):
            mapper.map_path('E:/work/file', required=True)
        self.assertEqual(mapper.resolve_link('E:/work/file')['status'], 'ambiguous')

    def test_markdown_precise_labels_code_prose_reference_images_and_suffix(self):
        old = 'E:/work/报告 (final).pdf'
        text = f'Prose {old}\n`[code](<{old}>)`\n```md\n[code](<{old}>)\n```\n[label](<{old}:12>) and ![image](<{old}#page=2>)\n[ref][doc]\n[doc]: <{old}> "Title"\n'
        changed, records = repair_markdown(text, self.mapper)
        expected = text.replace(f'[label](<{old}:12>)', f'[label](<{self.file.as_posix()}:12>)').replace(f'![image](<{old}#page=2>)', f'![image](<{self.file.as_posix()}#page=2>)').replace(f'[doc]: <{old}>', f'[doc]: <{self.file.as_posix()}>')
        self.assertEqual(changed, expected)
        self.assertEqual(sum(r.get('new') is not None for r in records), 3)

    def test_balanced_parentheses_escaped_and_repeated_destinations(self):
        old = 'E:/work/报告 (final).pdf'
        text = '[x](E:/work/报告%20(final).pdf) [y](E:/work/报告\\ \\(final\\).pdf)'
        output, records = repair_markdown(text, self.mapper)
        self.assertEqual(output, f'[x](<{self.file.as_posix()}>) [y](<{self.file.as_posix()}>)')
        self.assertEqual(len(records), 2)

    def test_missing_and_external_markdown_stay_unchanged(self):
        text = '[missing](E:/work/missing) [web](https://example.invalid/a)'
        result, records = repair_markdown(text, self.mapper)
        self.assertEqual(result, text)
        self.assertEqual({r['status'] for r in records}, {'missing', 'external-url'})

    def test_native_user_input_local_media_skill_and_mention_paths(self):
        old = 'E:/work/报告 (final).pdf'
        kinds = ('localImage', 'localAudio', 'skill', 'mention')
        item = {'id': 'synthetic-user', 'type': 'userMessage',
                'content': [{'type': kind, 'path': old, 'name': 'synthetic'} for kind in kinds]
                           + [{'type': 'unknownFutureInput', 'path': old}]}
        updated, changes = transform_item(item, self.mapper)
        self.assertEqual([value['path'] for value in updated['content'][:-1]], [self.file.as_posix()] * 4)
        self.assertEqual(updated['content'][-1]['path'], old)
        self.assertEqual(item['content'][0]['path'], old)
        self.assertEqual(len(changes), 4)

    def test_native_text_elements_follow_utf8_offsets_and_added_wrappers(self):
        simple = self.target / 'simple.txt'
        simple.write_text('synthetic')
        text = '前缀 [a](E:/work/simple.txt) 后缀 🔬 @token'
        start = len(text[:text.index('@token')].encode('utf-8'))
        item = {'type': 'userMessage', 'content': [{'type': 'text', 'text': text,
                'text_elements': [{'byteRange': {'start': 0, 'end': len('前缀'.encode())}, 'placeholder': 'prefix'},
                                  {'byteRange': {'start': start, 'end': start + len('@token')}, 'placeholder': 'mention'}]}]}
        updated, changes = transform_item(item, self.mapper)
        content = updated['content'][0]
        expected = '前缀 [a](<' + simple.as_posix() + '>) 后缀 🔬 @token'
        self.assertEqual(content['text'], expected)
        self.assertEqual(content['text_elements'][0], item['content'][0]['text_elements'][0])
        span = content['text_elements'][1]['byteRange']
        self.assertEqual(content['text'].encode()[span['start']:span['end']].decode(), '@token')
        self.assertEqual(span['start'], len(expected[:expected.index('@token')].encode()))
        self.assertEqual(item['content'][0]['text_elements'][1]['byteRange']['start'], start)
        self.assertTrue(any(record.get('status') == 'offset' for record in changes))

    def test_overlapping_native_text_element_preserves_text_and_reports_unresolved(self):
        old = 'E:/work/报告 (final).pdf'
        text = '前缀 [report](<' + old + '>) 后缀'
        start = len(text[:text.index(old)].encode())
        element = {'byteRange': {'start': start, 'end': start + len(old.encode())}, 'placeholder': 'special'}
        item = {'type': 'userMessage', 'content': [{'type': 'text', 'text': text, 'text_elements': [element]}]}
        updated, changes = transform_item(item, self.mapper)
        self.assertEqual(updated, item)
        unresolved = [record for record in changes if record['status'] == 'unsupported-text-elements']
        self.assertEqual(len(unresolved), 1)
        self.assertIsNone(unresolved[0]['new'])
        self.assertEqual(unresolved[0]['proposed_new'], self.file.as_posix())

    def test_invalid_native_text_element_ranges_never_patch_text(self):
        text = '中 [report](<E:/work/报告 (final).pdf>)'
        for elements in (None, [{'byteRange': {'start': 1, 'end': 2}}],
                         [{'byteRange': {'start': 10, 'end': 2}}],
                         [{'byteRange': {'start': 0, 'end': 999999}}],
                         [{'byteRange': {'start': False, 'end': 0}}]):
            with self.subTest(elements=elements):
                item = {'type': 'userMessage', 'content': [{'type': 'text', 'text': text, 'text_elements': elements}]}
                updated, changes = transform_item(item, self.mapper)
                self.assertEqual(updated, item)
                self.assertTrue(any(record['status'] == 'unsupported-text-elements' for record in changes))

    def test_native_items_transform_only_known_text_and_attachment_paths(self):
        old = 'E:/work/报告 (final).pdf'
        original = {'id': 'item-synthetic', 'type': 'agentMessage', 'text': f'[report](<{old}>)', 'command': f'cat {old}', 'custom': {'text': f'[ignore](<{old}>)'}, 'inputImages': [{'path': old}], 'savedPath': old}
        updated, changes = transform_item(original, self.mapper)
        self.assertEqual(updated['text'], f'[report](<{self.file.as_posix()}>)')
        self.assertEqual(updated['inputImages'][0]['path'], self.file.as_posix())
        self.assertEqual(updated['savedPath'], self.file.as_posix())
        self.assertEqual(updated['custom'], original['custom'])
        self.assertEqual(updated['command'], original['command'])
        self.assertEqual(original['savedPath'], old)
        self.assertGreaterEqual(len(changes), 3)


if __name__ == '__main__':
    unittest.main()
