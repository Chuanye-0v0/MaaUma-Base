"""Release only images created during an explicitly scoped new-run process."""
import json
from pathlib import Path

from core.auto_trainer.pacer_storage import _io, _rglob

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}


class NewScreenshotLifecycle:
    def __init__(self, output_root):
        self.root = Path(output_root).resolve()
        from core.cut_portable_runtime import screenshot_output_allowed,portable_root
        if not screenshot_output_allowed(self.root):
            raise ValueError('不保留截图模式的运行目录必须位于包内runs。' if portable_root()
                else '不保留截图模式的运行目录必须位于E盘。')
        self.existing = set(self._images())

    def _images(self):
        for path in _rglob(self.root, '*'):
            if _io(path).is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                resolved = path.resolve()
                if resolved.is_relative_to(self.root):
                    yield resolved

    def _identity_references(self):
        references = set()
        for path in _rglob(self.root, 'session/larc_member_roster.json'):
            data = json.loads(_io(path).read_text(encoding='utf-8-sig'))
            for member in data.get('members', []):
                sources = list((member.get('portrait_sources') or {}).values())
                if member.get('portrait_source'):
                    sources.append(member['portrait_source'])
                references.update(Path(source['absolute_path']).resolve() for source in sources
                    if isinstance(source, dict) and source.get('absolute_path'))
        return references

    def adopt_resumed_identity_cache(self, run_root):
        """Keep an existing attempt's portraits while active; release on close.

        Only this attempt's generated portrait cache changes ownership. Other
        pre-existing files, including historical screenshots, stay protected.
        """
        run_root = Path(run_root).resolve()
        cache = (run_root / 'session' / 'member_portraits').resolve()
        if not run_root.is_relative_to(self.root) or not cache.is_relative_to(run_root):
            raise ValueError('resumed_identity_cache_outside_run')
        adopted = {path for path in self.existing if path.is_relative_to(cache)}
        self.existing.difference_update(adopted)
        return len(adopted)

    def release(self, *, keep_identity_references=True, current_run=None, device_port=None, captured_after=0):
        """Release owned images, including an explicitly adopted resume cache."""
        protected = self._identity_references() if keep_identity_references else set()
        result = {'removed': 0, 'identity_references_kept': 0}
        if current_run is not None:
            current_run=Path(current_run).resolve()
            if not current_run.is_relative_to(self.root):raise ValueError('diagnostic_run_outside_output_root')
            from core.auto_trainer.cut_stop_diagnostic import remember_recent_game_frame
            result['recent_game_frame']=remember_recent_game_frame(current_run,device_port,captured_after=captured_after)['status']
        for path in self._images():
            if path in self.existing:
                continue
            if path.name in {'stop.png','stop.jpg','recent.png'} and path.parent.name=='stop_diagnostic' and path.parent.parent.name=='session':
                result['stop_diagnostics_kept']=result.get('stop_diagnostics_kept',0)+1
                continue
            if path in protected or (keep_identity_references and 'member_portraits' in path.parts):
                result['identity_references_kept'] += 1
                continue
            # Each resolved file is inside this explicit output root and did
            # not exist at construction. This is not a historical cleanup.
            _io(path).unlink()
            result['removed'] += 1
        return result
