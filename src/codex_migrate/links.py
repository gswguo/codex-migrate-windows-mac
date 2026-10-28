"""Conservative, span-based Markdown and native item link repair."""
from __future__ import annotations
import copy
import re
from .paths import PathMapper


def _protected(text: str) -> list[tuple[int, int]]:
    spans = []
    offset = 0
    fence = None
    start = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker and fence is None:
            fence = marker.group(1)
            start = offset
        elif marker and fence and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
            spans.append((start, offset + len(line)))
            fence = None
        elif fence is None and (line.startswith('    ') or line.startswith('\t')):
            spans.append((offset, offset + len(line)))
        offset += len(line)
    if fence:
        spans.append((start, len(text)))
    for match in re.finditer(r'(`+)([\s\S]*?)\1(?!`)', text):
        spans.append(match.span())
    return spans


def _destination(text: str, start: int) -> tuple[int, int, int] | None:
    """Return destination start/end and position after it, without guessing titles."""
    pos = start
    while pos < len(text) and text[pos] in ' \t':
        pos += 1
    if pos >= len(text):
        return None
    if text[pos] == '<':
        end = pos + 1
        while end < len(text) and text[end] not in '>\n':
            end += 2 if text[end] == '\\' else 1
        return (pos + 1, end, end + 1) if end < len(text) and text[end] == '>' else None
    begin = pos
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char == '\\' and pos + 1 < len(text):
            pos += 2
            continue
        if char == '(':
            depth += 1
        elif char == ')':
            if depth == 0:
                break
            depth -= 1
        elif char.isspace() and depth == 0:
            break
        pos += 1
    return (begin, pos, pos) if pos > begin and depth == 0 else None


def _markdown_replacement(text: str, start: int, destination: str) -> str:
    # Share the exact wrapping rule with native byte-range remapping.
    if not (start and text[start - 1] == '<') and any(c in destination for c in ' ()'):
        return '<' + destination + '>'
    return destination


def _shift_text_elements(text: str, elements, records):
    """Shift native half-open UTF-8 byte ranges without changing their contents.

    An element intersecting a patched destination has unknown semantics. Refuse
    that entire text field instead of guessing a replacement range for it.
    """
    if not isinstance(elements, list):
        raise ValueError('text_elements must be an array')
    replacements = [(r['start'], r['end'], _markdown_replacement(text, r['start'], r['new']))
                    for r in records if r.get('new') is not None]
    # Convert only requested character positions, with one pass over text slices;
    # a per-character Python integer array would greatly amplify large events.
    positions = sorted({position for start, end, _ in replacements for position in (start, end)})
    byte_positions = {}
    character, byte = 0, 0
    for position in positions:
        byte += len(text[character:position].encode('utf-8'))
        byte_positions[position] = byte
        character = position
    patches = [(byte_positions[start], byte_positions[end],
                len(replacement.encode('utf-8')) - (byte_positions[end] - byte_positions[start]))
               for start, end, replacement in replacements]
    original_bytes = text.encode('utf-8')
    result, updates = copy.deepcopy(elements), []
    for index, element in enumerate(result):
        if not isinstance(element, dict) or not isinstance(element.get('byteRange'), dict):
            raise ValueError('Unknown native text element range')
        span = element['byteRange']
        start, end = span.get('start'), span.get('end')
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (start, end)) or not 0 <= start <= end <= len(original_bytes):
            raise ValueError('Invalid native text element byte range')
        if any(position < len(original_bytes) and original_bytes[position] & 0xC0 == 0x80 for position in (start, end)):
            raise ValueError('Native text element splits a UTF-8 character')
        if any((start < finish and end > begin) if start != end else (begin < start < finish)
               for begin, finish, _ in patches):
            raise ValueError('Native text element overlaps a rewritten Markdown destination')
        before = dict(span)
        span['start'] = start + sum(delta for _, finish, delta in patches if finish <= start)
        span['end'] = end + sum(delta for _, finish, delta in patches if finish <= end)
        if span != before:
            updates.append({'index': index, 'old': before, 'new': dict(span), 'status': 'offset'})
    return result, updates


def repair_markdown(text: str, mapper: PathMapper) -> tuple[str, list[dict]]:
    protected = _protected(text)
    def blocked(start, end):
        return any(start < b and end > a for a, b in protected)
    targets = []
    unsupported = []
    # Definitions retain their optional title and visible reference labels.
    for match in re.finditer(r'^ {0,3}\[(?:\\.|[^\]\\\n])+\]:[ \t]*', text, re.M):
        result = _destination(text, match.end())
        if result and not blocked(match.start(), result[2]):
            targets.append((result[0], result[1]))
    # A balanced label parser avoids rewriting examples or label text.
    pos = 0
    while pos < len(text):
        if text[pos] != '[' or (pos and text[pos - 1] == '\\') or blocked(pos, pos + 1):
            pos += 1
            continue
        end, depth = pos + 1, 1
        while end < len(text) and depth:
            if text[end] == '\\':
                end += 2
                continue
            depth += (text[end] == '[') - (text[end] == ']')
            end += 1
        if depth == 0 and end < len(text) and text[end] == '(':
            result = _destination(text, end + 1)
            if result:
                tail = result[2]
                closing = re.match(r'[ \t]*(?:"[^"\n]*"|\'[^\'\n]*\'|\([^\n]*\))?[ \t]*\)', text[tail:])
                if closing and not blocked(pos, tail + closing.end()):
                    targets.append((result[0], result[1]))
                    pos = tail + closing.end()
                    continue
            if not blocked(pos, end + 1):
                unsupported.append({'status': 'unsupported-syntax', 'old': text[end + 1:text.find('\n', end + 1) if '\n' in text[end + 1:] else len(text)], 'new': None, 'path': None, 'start': end + 1, 'end': end + 1, 'reason': 'unrecognized Markdown destination or closing syntax'})
        pos += 1
    records = unsupported
    patches = []
    for start, end in sorted(set(targets)):
        record = mapper.resolve_link(text[start:end])
        record.update({'start': start, 'end': end})
        replacement = record.get('new')
        if replacement is not None:
            patches.append((start, end, _markdown_replacement(text, start, replacement)))
        records.append(record)
    for start, end, replacement in reversed(patches):
        text = text[:start] + replacement + text[end:]
    return text, records


_TEXT_TYPES = {'agentMessage', 'userMessage', 'assistantMessage', 'agent_message', 'user_message', 'assistant_message', 'message'}


def transform_item(item: dict, mapper: PathMapper) -> tuple[dict, list[dict]]:
    result = copy.deepcopy(item)
    changes = []
    def text_field(container, key, field):
        if isinstance(container.get(key), str):
            original = container[key]
            updated, records = repair_markdown(original, mapper)
            span_records = []
            if updated != original and key == 'text' and 'text_elements' in container:
                try:
                    elements, span_records = _shift_text_elements(original, container['text_elements'], records)
                except ValueError as exc:
                    updated = original
                    records = [{**record, 'status': 'unsupported-text-elements',
                                'proposed_new': record['new'], 'new': None, 'reason': str(exc)}
                               if record.get('new') is not None else record for record in records]
                else:
                    container['text_elements'] = elements
            container[key] = updated
            changes.extend({**record, 'field': field, 'item_id': item.get('id')} for record in records)
            prefix = field.rsplit('.', 1)[0] + '.' if '.' in field else ''
            changes.extend({**record, 'field': prefix + 'text_elements.' + str(record['index']) + '.byteRange',
                            'item_id': item.get('id')} for record in span_records)
    def path_field(container, key, field):
        if isinstance(container.get(key), str):
            record = mapper.resolve_link(container[key])
            if record.get('new'):
                container[key] = record['new']
            changes.append({**record, 'field': field, 'item_id': item.get('id')})
    if result.get('type') in _TEXT_TYPES or result.get('kind') in _TEXT_TYPES or result.get('role') in {'user', 'assistant'}:
        for field in ('text', 'message'):
            text_field(result, field, field)
        if isinstance(result.get('content'), str):
            text_field(result, 'content', 'content')
        elif isinstance(result.get('content'), list):
            for index, content in enumerate(result['content']):
                if isinstance(content, dict) and content.get('type') in {'text', 'input_text', 'output_text'}:
                    text_field(content, 'text', f'content.{index}.text')
                elif isinstance(content, dict) and content.get('type') in {'image', 'input_image', 'local_image', 'localImage', 'localAudio', 'skill', 'mention', 'attachment'}:
                    for field in ('path', 'image_url', 'file_path'):
                        path_field(content, field, f'content.{index}.{field}')
    for field in ('savedPath', 'filePath'):
        path_field(result, field, field)
    for field in ('inputImages', 'attachments'):
        if isinstance(result.get(field), list):
            for index, value in enumerate(result[field]):
                if isinstance(value, dict):
                    for key in ('path', 'file_path', 'savedPath'):
                        path_field(value, key, f'{field}.{index}.{key}')
    return result, changes
