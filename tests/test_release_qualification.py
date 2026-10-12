"""Contract for signing artifacts without triggering release distribution."""
import hashlib
from pathlib import Path
import re

import pytest


WORKFLOW = Path(__file__).parents[1] / '.github/workflows/release.yml'
INPUT = '''    inputs:
      qualification_only:
        description: Build and sign artifacts without publishing or updating distribution
        type: boolean
        required: false
        default: false
'''
GUARD = "    if: ${{ github.event_name != 'workflow_dispatch' || inputs.qualification_only != true }}\n"


def jobs(source):
    body = source.split('\njobs:\n', 1)[1]
    starts = list(re.finditer(r'^  ([a-z][a-z0-9_-]*):\n', body, re.MULTILINE))
    return {match.group(1): body[match.end():starts[index + 1].start() if index + 1 < len(starts) else len(body)]
            for index, match in enumerate(starts)}


def condition_value(expression, event, qualification):
    """Evaluate only the workflow's closed comparison/or grammar, never code."""
    context = {'github.event_name': event, 'inputs.qualification_only': qualification}
    terms = expression.strip().removeprefix('${{').removesuffix('}}').split('||')
    values = []
    for term in terms:
        match = re.fullmatch(r"\s*(github\.event_name|inputs\.qualification_only)\s*(!=|==)\s*('workflow_dispatch'|true)\s*", term)
        assert match, 'Release publication guard must use the reviewed closed grammar'
        operand, operator, literal = match.groups()
        expected = True if literal == 'true' else 'workflow_dispatch'
        equal = context[operand] == expected
        values.append(equal if operator == '==' else not equal)
    return any(values)


@pytest.mark.parametrize(('event', 'value', 'publishes'), [
    ('workflow_dispatch', True, False), ('workflow_dispatch', False, True),
    ('workflow_dispatch', None, True), ('push', None, True), ('push', False, True),
    ('push', True, True)])
def test_only_explicit_qualification_dispatch_skips_publication(event, value, publishes):
    release = jobs(WORKFLOW.read_text())['release']
    expression = re.search(r'^    if: (.+)$', release, re.MULTILINE).group(1)
    assert condition_value(expression, event, value) is publishes


def test_dispatch_input_is_optional_boolean_and_defaults_to_normal_release():
    source = WORKFLOW.read_text()
    assert source.count('qualification_only:') == 1
    assert '  workflow_dispatch:\n' + INPUT in source
    assert '    tags:\n      - "v*"' in source


def test_entire_release_and_distribution_write_dependency_closure_is_guarded():
    blocks = jobs(WORKFLOW.read_text())
    writers = {name for name, body in blocks.items()
               if re.search(r'uses: softprops/action-gh-release|gh (?:release|workflow run)|git push', body)}
    assert writers == {'release'}
    assert 'needs: build' in blocks['release']
    assert GUARD.strip() in blocks['release']
    assert 'Create release' in blocks['release'] and 'Dispatch Homebrew tap update' in blocks['release']
    # WinGet and the tap's release-event paths require a published release.
    for filename in ('update-homebrew-tap.yml', 'update-winget.yml'):
        source = WORKFLOW.with_name(filename).read_text()
        assert re.search(r'  release:\n    types: \[published\]', source)
    # Signing/upload jobs remain runnable; no skipped publish job is needed by them.
    assert re.search(r'^    if:', blocks['build'], re.MULTILINE) is None
    assert re.search(r'^    needs:', blocks['build'], re.MULTILINE) is None
    assert 'uses: actions/upload-artifact@v4' in blocks['build']
    assert 'Verify Windows executable signature' in blocks['build']
    assert 'Verify Windows MSI signature' in blocks['build']


def test_all_original_build_sign_upload_and_default_release_bytes_are_preserved():
    source = WORKFLOW.read_text()
    assert source.count(INPUT) == 1 and source.count(GUARD) == 1
    original = source.replace(INPUT, '', 1).replace(GUARD, '', 1).encode()
    digest = hashlib.sha1(b'blob ' + str(len(original)).encode() + b'\0' + original).hexdigest()
    assert digest == 'b88e00dcb2bb4beb7597c2896f726efa808b75c6'
