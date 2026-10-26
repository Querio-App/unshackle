"""unshackle command line."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import click
import httpx

from unshackle import build, scaffold, verify, voyage
from unshackle.http import SourceError
from unshackle.lock import Lock
from unshackle.raw import RawStore
from unshackle.sources import TOOLS, get_source, source_class
from unshackle.sources.base import Source

REPO = click.option("--repo", type=click.Path(file_okay=False, path_type=Path), default=Path("."), show_default=True, help="Target repo.")
TOOL = click.argument("tool", type=click.Choice(sorted(TOOLS)))


@click.group()
@click.version_option(package_name="unshackle", prog_name="unshackle")
@click.option("--quiet", "-q", is_flag=True, help="Plain output: no banner or animation.")
@click.pass_context
def main(ctx: click.Context, quiet: bool) -> None:
    """Unshackle your BI tool: extract its definitions into open formats in a Git repo you own."""
    ctx.ensure_object(dict)["quiet"] = quiet


def _state() -> dict:
    return click.get_current_context().find_root().ensure_object(dict)


@contextmanager
def _voyage(*steps: str):
    """The ship animation for a command; nested commands (migrate) move the same ship on."""
    state = _state()
    if state.get("voyage"):
        state["voyage"].step(steps[0])
        yield state["voyage"]
        return
    if not (voyage.enabled(quiet=state.get("quiet", False)) and sys.stdout.isatty()):
        yield None
        return
    if not state.get("banner"):
        voyage.banner()
        state["banner"] = True
    with voyage.Voyage(list(steps)) as ship:
        state["voyage"] = ship
        try:
            yield ship
        finally:
            state["voyage"] = None
    if state.get("summary"):
        click.echo(state.pop("summary"), err=True)


def _say(message: str, err: bool = False) -> None:
    ship = _state().get("voyage")
    if ship:
        ship.say(message)
    else:
        click.echo(message, err=err)


@main.command()
def tools() -> None:
    """List supported tools, the environment variables each needs, and the install extra beyond `run`."""
    for name in TOOLS:
        cls = source_class(name)
        extra = f"  (install unshackle[run,{cls.extra}])" if cls.extra else ""
        if not cls.required_env:
            click.echo(f"{name:12} {', '.join(cls.env_vars)}{extra}")
            continue
        optional = [v for v in cls.env_vars if v not in cls.required_env]
        click.echo(f"{name:12} required: {', '.join(cls.required_env)}" + (f"; optional: {', '.join(optional)}" if optional else "") + extra)


@main.command()
@REPO
@click.option("--name", default=None)
@click.option("--update-skills", is_flag=True, help="Refresh migration/skills/ from this unshackle version.")
def init(repo: Path, name: str | None, update_skills: bool) -> None:
    """Create the unshackle layout in REPO and copy the migration skills (never overwrites)."""
    for rel in scaffold.init(repo, name, update_skills):
        _say(f"created {rel}")


@main.command()
@TOOL
@REPO
def inventory(tool: str, repo: Path) -> None:
    """List everything in TOOL, with usage, into migration/inventory.<tool>.md."""
    with _voyage("taking inventory"):
        inv = _run(lambda: get_source(tool).inventory())
    out = repo / "migration"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"inventory.{tool}.md").write_text(inv.to_markdown())
    (out / f"inventory.{tool}.json").write_text(json.dumps(inv.to_dict(), indent=2) + "\n")
    _say(f"{len(inv.items)} items -> migration/inventory.{tool}.md")


@main.command()
@TOOL
@REPO
@click.option("--only", multiple=True, help="Inventory ids to extract (repeatable). Default: everything.")
@click.option("--with-data", is_flag=True, help="Also copy data that lives only in the tool (Tableau extracts, Excel, CSV) to Parquet.")
def extract(tool: str, repo: Path, only: tuple[str, ...], with_data: bool = False) -> None:
    """Pull raw definitions into migration/raw/<tool>/ (no LLM, exact copies)."""
    src = get_source(tool)
    if with_data:
        if not hasattr(src, "with_data"):
            raise click.ClickException(f"--with-data: {tool} has no data of its own to copy")
        src.with_data = True
    store = RawStore(repo, tool)
    if not only:
        store.begin()
    with _voyage("extracting"):
        _run(lambda: src.extract(store, set(only) or None))
        store.save()
        if store.had_errors():
            _say(f"some items failed: see migration/raw/{tool}/_errors.json (earlier raw files were kept)", err=True)
        changes = Lock.load(repo).changed_sources(tool, store.manifest)
        _say(f"{len(store.manifest)} raw files, {len(changes)} added/changed/removed since last build")
    if with_data:
        click.echo(f"next: {src.data_next_steps(repo)}")


@main.command(name="import")
@TOOL
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@REPO
@click.option("--with-data", is_flag=True, help="Also copy data packaged in the files (Tableau .twbx/.tdsx extracts, Excel, CSV) to Parquet.")
def import_cmd(tool: str, paths: tuple[Path, ...], repo: Path, with_data: bool = False) -> None:
    """Use files downloaded from TOOL by hand (no API access) as migration/raw/<tool>/. Replaces earlier raw files."""
    from unshackle.sources._manual import run as import_files

    src = get_source(tool)
    if with_data:
        if not hasattr(src, "import_data"):
            raise click.ClickException(f"--with-data: {tool} files carry no data of their own to copy")
        src.with_data = True
    store = RawStore(repo, tool)
    before = len(store.content_files())
    with _voyage("importing"):
        files = _run(lambda: import_files(src, store, list(paths)))
        _say(f"{len(files)} raw files imported manually" + (f" (replacing {before} earlier raw files)" if before else ""))
    click.echo(f"next: unshackle build {tool}")
    if with_data:
        click.echo(f"then: {src.data_next_steps(repo)}")


@main.command(name="build")
@TOOL
@REPO
@click.option("--dry-run", is_flag=True)
def build_cmd(tool: str, repo: Path, dry_run: bool) -> None:
    """Parse raw files and write the unshackle layout. Keeps hand edits."""
    src = get_source(tool)
    store = RawStore(repo, tool)
    if not store.manifest:
        store.rescan()
        if store.manifest:
            store.save()
    if not store.content_files():
        hint = f" (extract errors: migration/raw/{tool}/_errors.json)" if store.had_errors() else ""
        raise click.ClickException(f"no raw files for {tool}{hint}; run `unshackle extract {tool}` (or `unshackle import {tool} <files>`) first")
    with _voyage("parsing", "writing") as ship:
        bundle = _run(lambda: src.parse(store))
        if ship:
            ship.step("writing")
        if not dry_run:
            from unshackle.resources import copy_skills

            copy_skills(repo)
        result = build.apply(bundle, repo, dry_run=dry_run)
        if not dry_run:
            lock = Lock.load(repo)
            lock.record_source(tool, store.manifest)
            lock.save()
        w = result.writes
        _say(f"{len(w.written)} written, {len(w.unchanged)} unchanged, {len(w.kept_hand_edits)} hand edits kept")
        if w.removed:
            _say(f"{len(w.removed)} no longer generated, {'would be ' if dry_run else ''}removed:")
            for rel in w.removed:
                _say(f"  {rel}")
        if w.kept_stale:
            _say(f"{len(w.kept_stale)} no longer generated, kept because edited (see report)")
        _say(f"report: {result.report_path}")
        if ship and not dry_run:
            todos = len(build.scan_todos(repo, build.all_generated(repo)))
            policies = len(bundle.permissions) + len(bundle.row_filters)
            _state()["summary"] = (f"{len(bundle.metrics)} metrics, {len(bundle.dashboards)} dashboards and {policies} policies "
                                   f"made it out of {tool}. {todos} TODOs left: unshackle todo")


@main.command()
@TOOL
@REPO
@click.option("--only", multiple=True)
@click.option("--with-data", is_flag=True, help="Also copy data that lives only in the tool to Parquet (see extract).")
def migrate(tool: str, repo: Path, only: tuple[str, ...], with_data: bool) -> None:
    """init + inventory + extract + build in one go."""
    _check_env(tool)
    ctx = click.get_current_context()
    with _voyage("setting up", "taking inventory", "extracting", "parsing", "writing"):
        ctx.invoke(init, repo=repo, name=None, update_skills=False)
        ctx.invoke(inventory, tool=tool, repo=repo)
        ctx.invoke(extract, tool=tool, repo=repo, only=only, with_data=with_data)
        ctx.invoke(build_cmd, tool=tool, repo=repo, dry_run=False)


def _check_env(tool: str) -> None:
    """Fail before anything is written when the API variables are missing, naming the import path if the tool has one."""
    cls = source_class(tool)
    missing = [v for v in cls.required_env if not os.environ.get(v)]
    if not missing:
        return
    message = f"missing environment variable{'s' if len(missing) > 1 else ''} {', '.join(missing)}"
    if cls.import_path is not Source.import_path:
        message += (f"\nno API credentials: to use files ({cls.manual_files}), run `unshackle import {tool} <files>`, "
                    f"then `unshackle build {tool}`")
    raise click.ClickException(message)


@main.command()
@REPO
def todo(repo: Path) -> None:
    """List everything still marked TODO(unshackle)."""
    items = build.scan_todos(repo, build.all_generated(repo))
    for rel, n, msg in items:
        click.echo(f"{rel}:{n}: {msg}")
    click.echo(f"{len(items)} left")


@main.command(name="verify")
@REPO
@click.option("--url", envvar="UNSHACKLE_WAREHOUSE_URL", default=None, help="SQLAlchemy URL of the warehouse.")
def verify_cmd(repo: Path, url: str | None) -> None:
    """Run migration/parity/*.yaml checks; passing ones become evals."""
    results = verify.run_checks(repo, url)
    if not results:
        click.echo("no checks in migration/parity/*.yaml (see skills/verify)")
    for r in results:
        click.echo(f"{r.status:12} {r.name} {r.note}")
    build.write_report(repo)
    if any(r.status != "pass" for r in results):
        raise SystemExit(1)


@main.command()
@REPO
def report(repo: Path) -> None:
    """Rebuild migration/report.md."""
    click.echo(build.write_report(repo))


@main.command()
@REPO
def status(repo: Path) -> None:
    """What changed in each source since the last build."""
    lock = Lock.load(repo)
    for tool in TOOLS:
        store = RawStore(repo, tool)
        if not store.manifest and tool not in lock.sources:
            continue
        changes = lock.changed_sources(tool, store.manifest)
        click.echo(f"{tool}: {len(changes)} changed")
        for k, v in sorted(changes.items()):
            click.echo(f"  {v:8} {k}")


def _run(fn):
    try:
        return fn()
    except SourceError as exc:
        raise click.ClickException(str(exc)) from exc
    except httpx.HTTPError as exc:
        host = ""
        try:
            host = f" reaching {exc.request.url.host}"
        except (AttributeError, RuntimeError):
            pass
        raise click.ClickException(f"network error{host}: {exc} (a 403 here can come from a proxy, not the BI tool)") from exc


if __name__ == "__main__":
    main()
