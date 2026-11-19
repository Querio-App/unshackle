"""One confined view shared by MCP tools and resources."""
from __future__ import annotations

import ast
from collections import Counter
import json
import os
import re
from pathlib import Path

import yaml

from unshackle.run import _source, notebook_queries, chart_spec_from_source
from unshackle.target.layout import Layout

KINDS = ('semantic', 'catalog', 'notebooks', 'knowledge', 'policies', 'queries', 'sql_models')
SUFFIXES = {'.yml', '.yaml', '.md', '.sql', '.py', '.cedar', '.ipynb', '.json'}
MAX_FILE_BYTES = 2_000_000


def _pending_sql(nb: dict, sqls: dict[str, str]) -> dict[str, dict]:
    """Retain SQL assignments the literal reader cannot resolve, never execute code.

    Any unresolved write invalidates a previously recovered literal for that tile:
    notebook execution order and runtime branches cannot be inferred statically.
    """
    pending = {}
    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'code':
            continue
        source = _source(cell)
        literals, sources = {}, {}
        try:
            tree = ast.parse(source)
            writes = Counter(node.id for node in ast.walk(tree)
                             if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
                             and node.id.endswith('_sql'))
            for node in ast.walk(tree):
                targets = (node.targets if isinstance(node, ast.Assign) else [node.target]
                           if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) else [])
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and name.id in writes:
                            sources.setdefault(name.id, []).append(ast.get_source_segment(source, node) or name.id)
            for node in tree.body:
                target = (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1
                          else node.target if isinstance(node, ast.AnnAssign) else None)
                if not isinstance(target, ast.Name) or target.id not in writes:
                    continue
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if writes[target.id] == 1 and isinstance(value, str) and value.strip():
                    literals[target.id] = value
        except SyntaxError:
            # Preserve only lines naming SQL variables, not unrelated setup or
            # credential assignments elsewhere in a malformed cell.
            writes = dict.fromkeys(re.findall(r'\b([A-Za-z_]\w*_sql)\b', source))
            for name in writes:
                sources[name] = [line for line in source.splitlines() if re.search(rf'\b{re.escape(name)}\b', line)]
        for name in writes:
            ident = name[:-4]
            if name in literals:
                sqls[ident] = literals[name]
                continue
            item = pending.setdefault(ident, {
                'source_expression': '',
                'todo': 'TODO(unshackle): notebook SQL cannot be recovered as one unconditional literal; inspect source_expression',
            })
            expression = '\n'.join(sources.get(name, [name]))
            item['source_expression'] += ('\n\n' if item['source_expression'] else '') + expression
    for ident in pending:
        sqls[ident] = ''
    return pending


class Repository:
    def __init__(self, repo: Path):
        self.root = repo.resolve(strict=True)
        # Layout itself is input, but cannot be redirected outside the snapshot.
        for name in ('unshackle.toml', 'dbt_project.yml'):
            if (self.root / name).is_symlink():
                raise ValueError('Repository configuration cannot be a symlink')
        self.layout = Layout.load(self.root)
        self.roots = {}
        for kind in KINDS:
            p = Path(self.layout.paths[kind])
            if p.is_absolute() or '..' in p.parts or not p.parts or any(x.startswith('.') for x in p.parts):
                raise ValueError('MCP layout paths must be non-hidden relative directories')
            resolved = (self.root / p).resolve()
            if self.root not in resolved.parents:
                raise ValueError('MCP layout escapes repository')
            if p.parts[0] in {'migration', 'data'}:
                raise ValueError('Raw exports and local data are not MCP resources')
            self.roots[kind] = p

    def path(self, relative: str) -> Path:
        p = Path(relative)
        if p.is_absolute() or '..' in p.parts or any(x.startswith('.') for x in p.parts):
            raise ValueError('Path is outside the MCP allowlist')
        if p.suffix.lower() not in SUFFIXES or p.name.lower() in {'credentials.json', 'secrets.json', 'credentials.yaml', 'secrets.yaml'}:
            raise ValueError('File type is outside the MCP allowlist')
        if not any(root in p.parents for root in self.roots.values()):
            raise ValueError('Path is outside the MCP allowlist')
        resolved = (self.root / p).resolve(strict=True)
        # Disallow symlinks entirely, including in-repo links to secret/raw files.
        if any((self.root / Path(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts) + 1)):
            raise ValueError('Symlink resources are not allowed')
        if self.root not in resolved.parents or not resolved.is_file():
            raise ValueError('Path is outside the repository')
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('File exceeds MCP resource size limit')
        return resolved

    def files(self) -> list[str]:
        paths = set()
        for root in self.roots.values():
            for p in (self.root / root).rglob('*'):
                rel = p.relative_to(self.root).as_posix()
                try:
                    self.path(rel)
                except (ValueError, OSError):
                    continue
                paths.add(rel)
        return sorted(paths)

    def read(self, path: str) -> dict:
        actual = self.path(path)
        content = actual.read_text(encoding='utf-8')
        result = {'path': path, 'content': content, 'has_todo': 'TODO(unshackle)' in content}
        if actual.suffix == '.ipynb':
            nb = json.loads(content)
            params, sqls = notebook_queries(actual)
            pending = _pending_sql(nb, sqls)
            tiles = []
            for ident, sql in sqls.items():
                chart = None
                for cell in nb.get('cells', []):
                    src = _source(cell)
                    if cell.get('cell_type') != 'code' or f'{ident}_chart' not in src:
                        continue
                    meta = cell.get('metadata', {}).get('unshackle', {})
                    chart = meta.get('chart_spec') if meta.get('chart_source') == src else None
                    if chart is None:
                        chart = chart_spec_from_source(src, ident)
                    break
                tiles.append({'id': ident, 'sql': sql, 'chart_spec': chart, **pending.get(ident, {})})
            result = {'path': path, 'parameters': params, 'tiles': tiles,
                      'has_todo': result['has_todo'] or 'TODO(unshackle)' in json.dumps(tiles)}
            result['datasource'] = nb.get('metadata', {}).get('unshackle', {}).get('datasource', '')
        datasource = result.get('datasource', '')
        if not datasource:
            match = re.search(r'(?m)^(?:-- |# )?datasource:\s*([^\n]+)', content)
            datasource = match.group(1).strip() if match else ''
        if not datasource and self.roots['catalog'] in Path(path).parents:
            parts = Path(path).relative_to(self.roots['catalog']).parts
            datasource = parts[0] if len(parts) >= 3 else ''
        if datasource:
            result['datasource'] = datasource
            result['connection_warning'] = ('File datasource is context only; run_query always uses the server warehouse. '
                                            'Verify this file belongs to that connection.')
        return result

    def search(self, query: str) -> list[dict]:
        if not query.strip():
            raise ValueError('Search query cannot be empty')
        results = []
        for path in self.files():
            content = self.path(path).read_text(encoding='utf-8')
            for number, line in enumerate(content.splitlines(), 1):
                if query.casefold() in line.casefold():
                    results.append({'path': path, 'snippet': f'{number}: {line[:500]}',
                                    'has_todo': self.read(path)['has_todo']})
                    if len(results) >= 100:
                        return results
        return results

    def overview(self) -> dict:
        files = self.files()
        counts = {'models': 0, 'metrics': 0, 'notebooks': 0, 'tables': 0, 'todos': 0}
        model_names, metric_names = set(), set()
        for path in files:
            content = self.path(path).read_text(encoding='utf-8')
            counts['todos'] += content.count('TODO(unshackle)')
            p = Path(path)
            if self.roots['notebooks'] in p.parents and p.suffix == '.ipynb':
                counts['notebooks'] += 1
            if p.suffix not in {'.yml', '.yaml'}:
                continue
            if self.roots['catalog'] in p.parents:
                counts['tables'] += 1
            if self.roots['semantic'] in p.parents:
                try:
                    doc = yaml.safe_load(content)
                    if isinstance(doc, dict):
                        for model in doc.get('semantic_models', []) + doc.get('datasets', []):
                            if isinstance(model, dict):
                                model_names.add(model.get('name'))
                                metric_names.update(m.get('name') for m in model.get('metrics', []) if isinstance(m, dict))
                        metric_names.update(m.get('name') for m in doc.get('metrics', []) if isinstance(m, dict))
                except yaml.YAMLError:
                    pass
        counts['models'] = len(model_names - {None})
        counts['metrics'] = len(metric_names - {None})
        url = os.environ.get('UNSHACKLE_WAREHOUSE_URL')
        engine = None
        if url:
            from sqlalchemy.engine import make_url
            try:
                engine = make_url(url).get_backend_name()
            except Exception:
                engine = 'invalid URL'
        return {**counts, 'files': files, 'warehouse': {'configured': bool(url), 'connection_verified': False, 'engine': engine},
                'datasources': self.layout.warehouses,
                'warnings': ['One warehouse role per server; policies are context and are not enforced.',
                             'Files with another datasource require that connection; verify file provenance.',
                             'Git-ignored local data is not present in Git-hosted snapshots.']}
