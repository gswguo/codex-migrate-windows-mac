"""Synthetic ordinal histories; positions are independently read from final bytes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

from codex_migrate import history

from codex_migrate.history import rewrite_histories
from codex_migrate.paths import PathMapper
from codex_migrate.errors import MigrationError


def line(event):
    return json.dumps(event, ensure_ascii=False).encode() + b'\n'


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'destination-longer-项目'
        self.project.mkdir()
        (self.project / 'f.pdf').write_bytes(b'fiction')
        self.mapper = PathMapper([('E:/p', str(self.project))])

    def rollout(self, sid, events):
        path = self.root / (sid + '.jsonl')
        path.write_bytes(b''.join(line(e) for e in events))
        return path

    def events(self, sid, base=None):
        meta = {'id': sid, 'cwd': 'E:/p', 'parent_thread_id': 'identity-preserved'}
        if base:
            meta['history_base'] = base
        return [{'ordinal': 10, 'type': 'session_meta', 'payload': meta}, {'ordinal': 12, 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '[report](E:/p/f.pdf)'}}]

    def test_rebuilds_turn_projection_offsets_after_all_transformations(self):
        events = self.events('parent')
        source = self.rollout('parent', events)
        rows = {'thread_turns': [{'thread_id': 'parent', 'id': 'turn', 'rollout_ordinal': 10, 'rollout_byte_offset': 0, 'rollout_end_ordinal': 12, 'rollout_end_byte_offset': 0}], 'thread_history_projection_state': [{'thread_id': 'parent', 'next_rollout_ordinal': 12, 'next_rollout_byte_offset': 0}], 'thread_items': [{'thread_id': 'parent', 'id': 'item', 'item_json': json.dumps({'type': 'agentMessage', 'text': '[report](E:/p/f.pdf)'})}]}
        before = copy.deepcopy(rows)
        result = rewrite_histories({'parent': source}, self.root / 'out', self.mapper, rows)
        self.assertEqual(set(result), {'rollouts', 'history_rows', 'changes', 'counts'})
        output = Path(result['rollouts']['parent']).read_bytes()
        chunks = output.splitlines(keepends=True)
        parsed = [json.loads(c) for c in chunks]
        self.assertEqual([e['ordinal'] for e in parsed], [10, 12])
        self.assertEqual(parsed[0]['payload']['cwd'], self.project.as_posix())
        self.assertIn((self.project / 'f.pdf').as_posix(), parsed[1]['payload']['message'])
        turn = result['history_rows']['thread_turns'][0]
        self.assertEqual(turn['rollout_byte_offset'], 0)
        self.assertEqual(turn['rollout_end_byte_offset'], len(output))
        self.assertEqual(result['history_rows']['thread_history_projection_state'][0]['next_rollout_byte_offset'], len(chunks[0]))
        self.assertIn(self.project.as_posix(), result['history_rows']['thread_items'][0]['item_json'])
        self.assertEqual(rows, before)
        self.assertEqual(source.read_bytes(), b''.join(line(e) for e in events))

    def test_realtime_projection_items_receive_the_same_link_transformations(self):
        source=self.rollout('a',self.events('a'))
        item={'type':'agentMessage','text':'[report](E:/p/f.pdf)'}
        rows={'thread_realtime_items':[{'thread_id':'a','item_id':'synthetic-realtime-item','rollout_ordinal':12,'item_json':json.dumps(item)}]}
        result=rewrite_histories({'a':source},self.root/'realtime-out',self.mapper,rows)
        updated=json.loads(result['history_rows']['thread_realtime_items'][0]['item_json'])
        self.assertIn((self.project/'f.pdf').as_posix(),updated['text'])
        self.assertEqual(result['history_rows']['thread_realtime_items'][0]['rollout_ordinal'],12)
        self.assertEqual(json.loads(rows['thread_realtime_items'][0]['item_json']),item)

    def test_realtime_projection_missing_rollout_is_refused_before_staging(self):
        source=self.rollout('a',self.events('a'))
        rows={'thread_realtime_items':[{'thread_id':'synthetic-outside','item_id':'synthetic-item','item_json':'{}'}]}
        with self.assertRaises(MigrationError): rewrite_histories({'a':source},self.root/'realtime-missing',self.mapper,rows)
        self.assertFalse((self.root/'realtime-missing').exists())

    def test_multigeneration_all_metadata_bases_and_gap_boundaries(self):
        parent = self.rollout('parent', self.events('parent'))
        child_events = self.events('child', {'thread_id': 'parent', 'end_ordinal_exclusive': 11, 'end_byte_offset': 1})
        child_events.append({'ordinal': 15, 'type': 'session_meta', 'payload': {'id': 'child', 'history_base': {'thread_id': 'child', 'end_ordinal_exclusive': 15, 'end_byte_offset': 1}}})
        child = self.rollout('child', child_events)
        grand = self.rollout('grand', self.events('grand', {'thread_id': 'child', 'end_ordinal_exclusive': 99, 'end_byte_offset': 1}))
        result = rewrite_histories({'grand': grand, 'child': child, 'parent': parent}, self.root / 'out', self.mapper, {})
        p = Path(result['rollouts']['parent']).read_bytes().splitlines(keepends=True)
        c = Path(result['rollouts']['child']).read_bytes().splitlines(keepends=True)
        g = json.loads(Path(result['rollouts']['grand']).read_bytes().splitlines()[0])
        self.assertEqual(json.loads(c[0])['payload']['history_base']['end_byte_offset'], len(p[0]))
        self.assertEqual(json.loads(c[2])['payload']['history_base']['end_byte_offset'], len(c[0]) + len(c[1]))
        self.assertEqual(g['payload']['history_base']['end_byte_offset'], sum(map(len, c)))
        self.assertEqual(json.loads(c[0])['payload']['parent_thread_id'], 'identity-preserved')

    def test_unknown_native_permissions_fail_and_remove_partial_stage(self):
        events = self.events('a')
        events.append({'ordinal': 15, 'type': 'turn_context', 'payload': {'cwd': 'E:/p', 'sandbox_policy': {'type': 'unknown-new-policy'}}})
        source = self.rollout('a', events)
        destination = self.root / 'failed'
        with self.assertRaises(MigrationError):
            rewrite_histories({'a': source}, destination, self.mapper, {})
        self.assertFalse(destination.exists())

    def test_row_offset_updates_are_reported_and_open_turn_stays_open(self):
        source = self.rollout('a', self.events('a'))
        rows = {'thread_turns': [{'thread_id': 'a', 'rollout_ordinal': 12, 'rollout_byte_offset': 2, 'rollout_end_ordinal': None, 'rollout_end_byte_offset': 4}]}
        result = rewrite_histories({'a': source}, self.root / 'out', self.mapper, rows)
        self.assertIsNone(result['history_rows']['thread_turns'][0]['rollout_end_byte_offset'])
        fields = {r['field'] for r in result['changes'] if r.get('table') == 'thread_turns'}
        self.assertEqual(fields, {'rollout_byte_offset', 'rollout_end_byte_offset'})

    def test_cycle_missing_ancestor_forward_self_and_duplicate_ordinals_fail(self):
        bad_cases = [({'thread_id': 'missing', 'end_ordinal_exclusive': 12}, None), ({'thread_id': 'a', 'end_ordinal_exclusive': 12}, None), ({'thread_id': 'b', 'end_ordinal_exclusive': 12}, {'thread_id': 'a', 'end_ordinal_exclusive': 12})]
        for index, (base_a, base_b) in enumerate(bad_cases):
            with self.subTest(index=index):
                sources = {'a': self.rollout('a', self.events('a', base_a))}
                if base_b:
                    sources['b'] = self.rollout('b', self.events('b', base_b))
                with self.assertRaises(MigrationError):
                    rewrite_histories(sources, self.root / f'bad-{index}', self.mapper, {})
        events = self.events('duplicate')
        events[1]['ordinal'] = 10
        with self.assertRaises(MigrationError):
            rewrite_histories({'duplicate': self.rollout('duplicate', events)}, self.root / 'dup-out', self.mapper, {})

    def test_required_cwd_mapping_and_unknown_offset_semantics_fail(self):
        events = self.events('a')
        events[0]['payload']['cwd'] = 'Q:/unknown'
        with self.assertRaises(MigrationError):
            rewrite_histories({'a': self.rollout('a', events)}, self.root / 'bad-cwd', self.mapper, {})
        source = self.rollout('b', self.events('b'))
        with self.assertRaises(MigrationError):
            rewrite_histories({'b': source}, self.root / 'bad-offset', self.mapper, {'mystery': [{'thread_id': 'b', 'unknown_byte_offset': 4}]})

    def test_unknown_history_base_offset_variant_and_index_offset_are_rejected(self):
        events = self.events('a')
        events.append({'ordinal': 20, 'type': 'session_meta', 'payload': {'history_base': {'thread_id': 'a', 'end_ordinal_exclusive': 12, 'end_byte_offset': 0, 'alternate_offset': 55}}})
        with self.assertRaises(MigrationError):
            rewrite_histories({'a': self.rollout('a', events)}, self.root / 'bad-base', self.mapper, {})
        source = self.rollout('b', self.events('b'))
        with self.assertRaises(MigrationError):
            rewrite_histories({'b': source}, self.root / 'bad-index', self.mapper, {'unknown': [{'thread_id': 'b', 'rollout_offset': 4}]})

    def test_streams_rollouts_without_retaining_decoded_events(self):
        events = self.events('a')
        events.extend({'ordinal': ordinal, 'type': 'unknown_event', 'payload': {'data': 'x' * 4096}}
                      for ordinal in range(20, 120))
        source = self.rollout('a', events)
        alive = weakref.WeakSet()
        original_loads = json.loads
        class Event(dict):
            __hash__ = object.__hash__
        def tracked(raw, *args, **kwargs):
            value = original_loads(raw, *args, **kwargs)
            if isinstance(value, dict) and 'ordinal' in value:
                value = Event(value)
                alive.add(value)
                self.assertLessEqual(len(alive), 6, 'decoded rollout events accumulated in memory')
            return value
        with patch.object(history.json, 'loads', tracked), patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file rollout read')):
            result = rewrite_histories({'a': source}, self.root / 'streamed', self.mapper, {})
        self.assertEqual(result['counts']['events'], len(events))
        with Path(result['rollouts']['a']).open('rb') as stream:
            self.assertEqual(sum(1 for _ in stream), len(events))

    def test_rewrite_rejects_source_modified_during_second_pass(self):
        source = self.rollout('a', self.events('a'))
        original = history._transform_event
        changed = False
        def mutate(event, mapper):
            nonlocal changed
            if not changed:
                changed = True
                source.write_bytes(line({'ordinal': 10, 'type': 'session_meta', 'payload': {'id': 'a', 'cwd': 'E:/p'}}))
            return original(event, mapper)
        destination = self.root / 'changed-input'
        with patch.object(history, '_transform_event', mutate):
            with self.assertRaises(MigrationError):
                rewrite_histories({'a': source}, destination, self.mapper, {})
        self.assertFalse(destination.exists())

    def test_oversized_single_event_is_refused_before_decoding(self):
        events = [{'ordinal': 0, 'type': 'session_meta', 'payload': {'id': 'a'}},
                  {'ordinal': 1, 'type': 'unknown', 'payload': {'data': 'x' * 1024}}]
        source = self.rollout('a', events)
        with patch.object(history, 'MAX_EVENT_BYTES', 128, create=True):
            with self.assertRaises(MigrationError) as caught:
                rewrite_histories({'a': source}, self.root / 'oversized', self.mapper, {})
        self.assertEqual(caught.exception.code, 'planning_limit')
        self.assertFalse((self.root / 'oversized').exists())

    def test_unsupported_projection_text_span_aborts_all_replay_rewrites(self):
        text = '[report](E:/p/f.pdf)'
        start = len('[report](')
        item = {'id': 'same-item', 'type': 'userMessage', 'content': [
            {'type': 'text', 'text': text, 'text_elements': [
                {'byteRange': {'start': start, 'end': start + len('E:/p/f.pdf')}}]}]}
        events = self.events('a')[:1]
        events.append({'ordinal': 12, 'type': 'response_item', 'payload': {
            'id': 'same-item', 'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': text}]}})
        source = self.rollout('a', events)
        rows = {'thread_items': [{'thread_id': 'a', 'item_id': 'same-item', 'item_json': json.dumps(item)}]}
        destination = self.root / 'unsafe-spans'
        with self.assertRaises(MigrationError) as caught:
            rewrite_histories({'a': source}, destination, self.mapper, rows)
        self.assertEqual(caught.exception.code, 'unsupported_text_elements')
        self.assertFalse(destination.exists())
        self.assertEqual(json.loads(rows['thread_items'][0]['item_json']), item)

    def test_native_completed_item_and_projection_keep_identical_utf8_spans(self):
        text = '中文 [report](E:/p/f.pdf) @special'
        start = len(text[:text.index('@special')].encode())
        item = {'id': 'same-item', 'type': 'userMessage', 'content': [
            {'type': 'text', 'text': text, 'text_elements': [
                {'byteRange': {'start': start, 'end': start + len('@special')}}]},
            {'type': 'localImage', 'path': 'E:/p/f.pdf'}]}
        events = self.events('a')[:1]
        events.append({'ordinal': 12, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': item}})
        source = self.rollout('a', events)
        rows = {'thread_items': [{'thread_id': 'a', 'item_id': 'same-item', 'item_json': json.dumps(item)}]}
        result = rewrite_histories({'a': source}, self.root / 'consistent-spans', self.mapper, rows)
        with Path(result['rollouts']['a']).open('rb') as stream:
            completed = [json.loads(raw) for raw in stream][-1]['payload']['item']
        projected = json.loads(result['history_rows']['thread_items'][0]['item_json'])
        self.assertEqual(completed, projected)
        content = projected['content'][0]
        span = content['text_elements'][0]['byteRange']
        self.assertEqual(content['text'].encode()[span['start']:span['end']].decode(), '@special')
        self.assertEqual(projected['content'][1]['path'], (self.project / 'f.pdf').as_posix())

    def test_context_ancestor_is_copied_exactly_and_only_affected_rows_return(self):
        parent_events = self.events('parent')
        parent_events[0]['payload']['cwd'] = 'Q:/unmapped-context'
        parent_events[0]['payload']['sandbox_policy'] = {'type': 'unknown-context-policy'}
        parent = self.root / 'parent-context.jsonl'
        first = json.dumps(parent_events[0], ensure_ascii=True).encode() + b'\r\n'
        original = first + json.dumps(parent_events[1], ensure_ascii=True).encode()
        parent.write_bytes(original)
        child = self.rollout('child', self.events('child', {
            'thread_id': 'parent', 'end_ordinal_exclusive': 12, 'end_byte_offset': 1}))
        rows = {'thread_turns': [
                    {'thread_id': 'parent', 'rollout_ordinal': 10, 'rollout_end_ordinal': 12},
                    {'thread_id': 'child', 'rollout_ordinal': 10, 'rollout_end_ordinal': 12}],
                'thread_items': [
                    {'thread_id': sid, 'item_json': json.dumps({'type': 'agentMessage', 'text': '[report](E:/p/f.pdf)'})}
                    for sid in ('parent', 'child')]}
        result = rewrite_histories({'child': child, 'parent': parent}, self.root / 'context-output',
                                   self.mapper, rows, transform_thread_ids={'child'})
        self.assertEqual(Path(result['rollouts']['parent']).read_bytes(), original)
        updated = [json.loads(raw) for raw in Path(result['rollouts']['child']).read_bytes().splitlines()]
        self.assertEqual(updated[0]['payload']['history_base']['end_byte_offset'], len(first))
        self.assertEqual(updated[0]['payload']['cwd'], self.project.as_posix())
        self.assertIn((self.project / 'f.pdf').as_posix(), updated[1]['payload']['message'])
        self.assertEqual([row['thread_id'] for row in result['history_rows']['thread_turns']], ['child'])
        self.assertEqual([row['thread_id'] for row in result['history_rows']['thread_items']], ['child'])
        self.assertEqual({change['thread_id'] for change in result['changes']}, {'child'})
        self.assertEqual(len(rows['thread_turns']), 2)

    def test_untransformed_descendant_of_affected_rollout_is_rejected(self):
        parent = self.rollout('parent', self.events('parent'))
        child = self.rollout('child', self.events('child', {
            'thread_id': 'parent', 'end_ordinal_exclusive': 12, 'end_byte_offset': 1}))
        destination = self.root / 'unsafe-closure'
        with self.assertRaises(MigrationError) as caught:
            rewrite_histories({'parent': parent, 'child': child}, destination, self.mapper, {},
                              transform_thread_ids={'parent'})
        self.assertEqual(caught.exception.code, 'invalid_selection')
        self.assertFalse(destination.exists())

    def test_final_runtime_paths_and_unmapped_permissions_are_restricted(self):
        events = self.events('a')
        events.append({'ordinal': 20, 'type': 'turn_context', 'payload': {'cwd': 'E:/p', 'workspace_roots': ['E:/p', 'Q:/unknown'], 'approval_policy': 'never', 'sandbox_policy': {'type': 'danger-full-access', 'network_access': True, 'writable_roots': ['E:/p', 'Q:/unknown']}}})
        result = rewrite_histories({'a': self.rollout('a', events)}, self.root / 'safe', self.mapper, {})
        context = json.loads(Path(result['rollouts']['a']).read_bytes().splitlines()[-1])['payload']
        self.assertEqual(context['workspace_roots'], [self.project.as_posix()])
        self.assertEqual(context['approval_policy'], 'on-request')
        self.assertEqual(context['sandbox_policy']['type'], 'workspace-write')
        self.assertFalse(context['sandbox_policy']['network_access'])


if __name__ == '__main__':
    unittest.main()
