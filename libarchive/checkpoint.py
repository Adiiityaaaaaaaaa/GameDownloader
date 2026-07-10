"""Resumable streaming extraction with on-disk checkpoints.

libarchive is a *forward-only* streaming decoder: it reads compressed bytes in
order, feeds them through a codec (for RAR: a large sliding-window dictionary,
often *solid* across files), and emits decoded entries. That decoder state lives
in native memory and is not serializable -- there is no way to snapshot it and
resume decoding from the middle of a stream later. So a crash or a closed
connection loses all in-flight progress.

This module adds the next best thing: **entry-boundary checkpointing**. As each
entry is fully written to disk we record it in a small JSON file (atomically).
When extraction is restarted from the beginning of the stream, entries that were
already extracted are *skipped* -- their bytes are still fed to the decoder (so
its state stays correct, via `ArchiveRead.skip_data`), but nothing is written to
disk again. Extraction therefore resumes at the first entry that wasn't finished.

What this gives you:

  * Safe, idempotent restarts. A hard kill can never leave a half-written file
    that looks complete: each entry is written to a temp file and atomically
    renamed, and only *then* recorded in the checkpoint. If interrupted, the
    unfinished entry is simply redone.
  * No redundant writes. Files completed before the interruption are not written
    again on resume.

What it does not (and cannot) give you:

  * It does not avoid re-reading the compressed stream. Because the decoder
    can't resume mid-stream, resuming re-reads the bytes of the skipped entries.
    For a plain forward HTTP download that means re-fetching them. If you need to
    avoid re-downloading, save the archive to disk and use a seekable reader, or
    resume the download itself with HTTP Range and extract afterwards.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .read import stream_reader
from .ffi import page_size

__all__ = ['Checkpoint', 'default_safe_path', 'checkpoint_stream_extract']


def default_safe_path(dest, pathname):
    """Resolve an archive entry path safely under `dest` (blocks traversal).

    Returns a `Path` under `dest`, or ``None`` if the entry has no usable name
    (e.g. it resolves entirely out of the destination). Absolute paths and
    ``..`` components are stripped rather than honored.
    """
    if not pathname:
        return None
    rel = str(pathname).replace('\\', '/').lstrip('/')
    parts = [p for p in rel.split('/') if p not in ('', '.', '..')]
    if not parts:
        return None
    return Path(dest).joinpath(*parts)


class Checkpoint:
    """An on-disk record of which archive entries have been fully extracted.

    The record is a JSON document persisted atomically to `path`. It is keyed by
    each entry's *ordinal position* in the archive (which is stable for a given
    archive), and also stores the entry's destination-relative path and size so
    a stale or mismatched checkpoint can be detected instead of trusted blindly.
    """

    VERSION = 1

    def __init__(self, path, source=None):
        self.path = Path(path)
        self.source = source          # opaque identity of the stream (e.g. URL)
        self.format = None            # archive format name, filled in on first use
        self._completed = []          # ordered list of {index, path, size, raw_offset}
        self._by_index = {}           # index -> record

    # ---- persistence ---------------------------------------------------

    @classmethod
    def load(cls, path, source=None):
        """Load a checkpoint from `path`, or return an empty one if absent.

        If `source` is given and the stored source differs, the checkpoint is
        treated as stale (a different archive is being extracted to the same
        location) and an empty checkpoint is returned instead.
        """
        cp = cls(path, source=source)
        try:
            data = json.loads(cp.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return cp
        if not isinstance(data, dict) or data.get('version') != cls.VERSION:
            return cp
        stored_source = data.get('source')
        if source is not None and stored_source is not None and stored_source != source:
            return cp  # stale: different archive, start fresh
        cp.source = stored_source if stored_source is not None else source
        cp.format = data.get('format')
        for rec in data.get('completed', []):
            if isinstance(rec, dict) and isinstance(rec.get('index'), int):
                cp._completed.append(rec)
                cp._by_index[rec['index']] = rec
        return cp

    def save(self):
        """Atomically write the checkpoint to disk."""
        payload = {
            'version': self.VERSION,
            'source': self.source,
            'format': self.format,
            'completed': self._completed,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self):
        """Delete the checkpoint file (call once extraction is complete)."""
        try:
            self.path.unlink()
        except OSError:
            pass

    # ---- queries / updates --------------------------------------------

    @property
    def completed_count(self):
        return len(self._completed)

    @property
    def resume_offset(self):
        """Raw (compressed) byte offset of the furthest completed entry.

        Useful for seeding a progress indicator when resuming: it approximates
        how far into the archive the already-extracted entries reach.
        """
        return max((rec.get('raw_offset') or 0 for rec in self._completed),
                   default=0)

    def is_done(self, index, rel_path, size):
        """True if entry `index` was already extracted with a matching path/size.

        Size is only compared when the record and the entry both carry one, so
        formats that don't report a size still resume correctly.
        """
        rec = self._by_index.get(index)
        if rec is None:
            return False
        if rec.get('path') != rel_path:
            return False
        rec_size = rec.get('size')
        if size is not None and rec_size is not None and rec_size != size:
            return False
        return True

    def mark(self, index, rel_path, size, raw_offset=None):
        rec = {'index': index, 'path': rel_path, 'size': size}
        if raw_offset is not None:
            rec['raw_offset'] = raw_offset
        if index not in self._by_index:
            self._completed.append(rec)
        else:
            # replace the existing record in-place to keep ordering stable
            for i, existing in enumerate(self._completed):
                if existing.get('index') == index:
                    self._completed[i] = rec
                    break
        self._by_index[index] = rec


def checkpoint_stream_extract(
    stream, dest, checkpoint, *,
    safe_path=default_safe_path, block_size=page_size, header_codec='utf-8',
):
    """Extract an archive from a forward-only `stream`, resuming from `checkpoint`.

    ``stream``      an object with a ``readinto(buf)`` method (and ``seekable()``
                    returning False for pure streaming). Progress reporting and
                    cancellation belong to this object -- see how rar-downloader's
                    ``_IterStream`` fires callbacks from ``readinto``.
    ``dest``        directory to extract into.
    ``checkpoint``  a :class:`Checkpoint`; updated and saved after each entry.
    ``safe_path``   ``(dest, pathname) -> Path | None`` resolver (traversal-safe).

    Returns the list of all entry paths that exist on disk afterwards, including
    ones skipped because they were already extracted. Entries already recorded in
    the checkpoint (and still present on disk with a matching size) are
    fast-forwarded with :meth:`ArchiveRead.skip_data` rather than rewritten.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []

    with stream_reader(
        stream, block_size=block_size, header_codec=header_codec
    ) as archive:
        for index, entry in enumerate(archive):
            if checkpoint.format is None:
                fmt = archive.format_name
                if isinstance(fmt, bytes):
                    fmt = fmt.decode('ascii', 'replace')
                checkpoint.format = fmt

            target = safe_path(dest, entry.pathname)
            if target is None:
                archive.skip_data()
                continue

            if entry.isdir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not entry.isreg:  # skip symlinks/devices/etc.
                archive.skip_data()
                continue

            try:
                rel_path = target.relative_to(dest).as_posix()
            except ValueError:
                rel_path = target.name

            # Already extracted and still on disk with the expected size? Feed
            # the decoder its bytes but don't rewrite the file.
            if checkpoint.is_done(index, rel_path, entry.size) and target.exists():
                if entry.size is None or target.stat().st_size == entry.size:
                    archive.skip_data()
                    written.append(target)
                    continue

            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.with_name(target.name + '.part-extract')
            try:
                n_written = 0
                with open(tmp_target, 'wb') as fh:
                    for block in entry.get_blocks(block_size):
                        fh.write(block)
                        n_written += len(block)
                # A stream that ends mid-entry (abort/truncation) must not be
                # committed or recorded, or resume would treat a short file as
                # complete. Only publish and checkpoint a fully-written entry.
                if entry.size is not None and n_written != entry.size:
                    raise OSError(
                        f"truncated entry {rel_path!r}: wrote {n_written} of "
                        f"{entry.size} bytes"
                    )
                os.replace(tmp_target, target)
            except BaseException:
                # Leave no half-written file behind on abort/error.
                try:
                    tmp_target.unlink()
                except OSError:
                    pass
                raise

            checkpoint.mark(
                index, rel_path, target.stat().st_size,
                raw_offset=archive.bytes_read,
            )
            checkpoint.save()
            written.append(target)

    return written
