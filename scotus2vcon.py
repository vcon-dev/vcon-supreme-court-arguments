#!/usr/bin/env python3
"""Supreme Court oral arguments to vCon.

Source is the Oyez API (https://api.oyez.org), which carries the Court's own
argument audio plus speaker-attributed, timestamped transcripts. When Oyez has
audio but no transcript, the MP3 is downloaded and transcribed locally with
mlx-whisper.

Conventions carried over from ietf2vcon (vcon-dev/ietf2vcon):
  - dialog type "recording", external url; analysis "wtf_transcription" with a
    string body and encoding "json"; vendor on every analysis
  - attachments use `purpose`, get party/dialog/start filled at build time
  - lawful_basis attachment with non-empty proof_mechanisms[]
  - party `role` declares the "role" extension; attachment/party meta declares "meta"
  - content_hash is sha512-<base64url, unpadded>, only when the bytes were fetched
  - output is idempotent: an existing vCon file is left alone unless --force

Usage:
  scotus2vcon.py --term 2023                # one term
  scotus2vcon.py --term 1955 --term-end 2025  # a range (Oyez audio starts 1955)
  scotus2vcon.py --docket 2023/22-451       # one case
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import warnings

import httpx

warnings.filterwarnings("ignore", module="authlib")
from vcon import Vcon  # noqa: E402
from vcon.dialog import Dialog
from vcon.party import Party

__version__ = "0.1.0"
OYEZ = "https://api.oyez.org"
HERE = Path(__file__).resolve().parent
log = logging.getLogger("scotus2vcon")


# ---------------------------------------------------------------- fetching

def content_hash_token(content: bytes) -> str:
    """`sha512-<base64url digest, unpadded>`, as core-02 requires."""
    digest = hashlib.sha512(content).digest()
    return "sha512-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


class Oyez:
    """Thin cached client. Every JSON response is kept under downloads/oyez."""

    def __init__(self, cache: Path):
        self.cache = cache
        self.http = httpx.Client(timeout=120, follow_redirects=True,
                                 headers={"User-Agent": f"scotus2vcon/{__version__}"})

    def _get(self, url: str, key: str) -> dict | list | None:
        path = self.cache / (key + ".json")
        if path.exists():
            return json.loads(path.read_text())
        for attempt in range(4):
            try:
                r = self.http.get(url)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                data = r.json()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
                return data
            except (httpx.HTTPError, ValueError) as e:
                log.warning("fetch %s failed (%s), retry %d", url, e, attempt + 1)
                time.sleep(2 ** attempt)
        return None

    def cases_for_term(self, term: int) -> list[dict]:
        return self._get(f"{OYEZ}/cases?per_page=0&filter=term:{term}", f"terms/{term}") or []

    def case(self, term: int | str, docket: str, case_id: int | None = None,
             href: str | None = None) -> dict | None:
        # Prefer the stub's own href: a reused docket is disambiguated as
        # "406_0", which the bare docket URL does not reach.
        url = href or f"{OYEZ}/cases/{term}/{docket}"
        data = self._get(url, f"cases/{term}/{url.rsplit('/', 1)[-1]}")
        if isinstance(data, list):
            # A docket reused across terms comes back as a list; pick by ID.
            data = next((c for c in data if case_id is None or c.get("ID") == case_id), None)
        if not isinstance(data, dict):  # Oyez has returned a bare int for some cases
            log.warning("unexpected case payload for %s/%s: %r", term, docket, data)
            return None
        return data

    def argument_audio(self, media_id: int) -> dict | None:
        return self._get(f"{OYEZ}/case_media/oral_argument_audio/{media_id}", f"audio/{media_id}")

    def download(self, url: str, dest: Path) -> bytes:
        if dest.exists():
            return dest.read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        return dest.read_bytes()


# ---------------------------------------------------------------- transcripts

def segments_from_oyez(transcript: dict, party_of: callable) -> tuple[list[dict], str, float]:
    """Flatten Oyez sections/turns/text_blocks into WTF segments."""
    segments: list[dict] = []
    end = 0.0
    for section in transcript.get("sections") or []:
        for turn in section.get("turns") or []:
            speaker = turn.get("speaker")
            party = party_of(speaker) if speaker else None
            for block in turn.get("text_blocks") or []:
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                seg = {
                    "id": len(segments),
                    "start": round(float(block["start"]), 3),
                    "end": round(float(block["stop"]), 3),
                    "text": text,
                    # Oyez transcripts are human-corrected court transcripts.
                    "confidence": 1.0,
                }
                if party is not None:
                    seg["speaker"] = party
                segments.append(seg)
                end = max(end, seg["end"])
    text = " ".join(s["text"] for s in segments)
    return segments, text, end


def transcribe_local(audio_path: Path, model: str) -> tuple[list[dict], str, float, str] | None:
    """mlx-whisper on Apple Silicon. Returns (segments, text, duration, language)."""
    try:
        import mlx_whisper  # noqa: PLC0415
    except ImportError:
        log.error("mlx-whisper not installed; pip install mlx-whisper")
        return None
    import math  # noqa: PLC0415

    log.info("local transcription: %s", audio_path.name)
    result = mlx_whisper.transcribe(str(audio_path), path_or_hf_repo=model, verbose=False)
    segments = []
    for i, seg in enumerate(result.get("segments", [])):
        conf = 0.95
        lp = seg.get("avg_logprob")
        if lp is not None and not math.isnan(lp):  # NaN would serialize as invalid JSON
            conf = min(math.exp(lp), 1.0)
        segments.append({
            "id": i,
            "start": round(seg["start"], 3),
            "end": round(seg["end"], 3),
            "text": seg["text"].strip(),
            "confidence": round(conf, 4),
        })
    duration = segments[-1]["end"] if segments else 0.0
    return segments, result.get("text", "").strip(), duration, result.get("language") or "en"


# ---------------------------------------------------------------- building

def epoch_iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z") if ts else None


def argued_dates(case: dict) -> list[str]:
    for ev in case.get("timeline") or []:
        if ev.get("event") == "Argued":
            return [epoch_iso(d) for d in ev.get("dates") or [] if d]
    return []


def parse_title_date(title: str | None) -> str | None:
    """'Oral Argument - January 17, 2024' -> 2024-01-17T00:00:00Z."""
    if not title:
        return None
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", title)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


class ArgumentVcon:
    def __init__(self):
        self.vcon = Vcon.build_new()
        self._parties: dict[str, int] = {}

    def _ext(self, name: str) -> None:
        if name not in (self.vcon.vcon_dict.get("extensions") or []):
            self.vcon.add_extension(name)

    def party(self, person: dict | None, role: str, description: str | None = None) -> int | None:
        if not person or not person.get("name"):
            return None
        key = str(person.get("ID") or person.get("identifier") or person["name"])
        if key in self._parties:
            return self._parties[key]
        meta = {"oyez": person.get("href")}
        title = next((r.get("role_title") for r in person.get("roles") or [] if r.get("role_title")), None)
        if title:
            meta["title"] = title
        if description:
            meta["description"] = description
        idx = len(self.vcon.parties)
        self.vcon.add_party(Party(name=person["name"], role=role, meta=meta))
        self._parties[key] = idx
        self._ext("role")
        self._ext("meta")
        return idx

    def build(self, case: dict, audio: dict, media: dict, transcript: tuple, provider: str,
              model: str, content_hash: str | None, language: str) -> Vcon:
        v = self.vcon
        segments, text, duration = transcript
        docket = case.get("docket_number")
        v.vcon_dict["subject"] = f"{case.get('name')} ({docket}) - {audio.get('title')}"

        start = parse_title_date(audio.get("title")) or (argued_dates(case) or [None])[0] \
            or v.vcon_dict["created_at"]
        dialog = Dialog(
            type="recording",
            start=start,
            duration=round(duration, 3) if duration else None,
            parties=list(range(len(v.parties))),
            mediatype=media.get("mime") or "audio/mpeg",
            url=media["href"],
            content_hash=content_hash,
            meta={"oyez_media_id": audio.get("id"), "source": "oyez"},
        )
        v.add_dialog(dialog)
        if content_hash is None:
            # ietf2vcon leaves stream URLs unhashed and says so. Here the file is
            # byte-stable, so --hash-audio can fill this in later.
            v.vcon_dict["dialog"][-1]["meta"]["content_hash_note"] = \
                "audio not fetched; rerun with --hash-audio to add content_hash"

        now = datetime.now(UTC).isoformat()
        v.add_analysis(
            type="wtf_transcription",
            dialog=0,
            vendor=provider,
            product=model,
            encoding="json",
            body=json.dumps({
                "transcript": {"text": text, "language": language,
                               "duration": round(duration, 3), "confidence": 1.0 if provider == "oyez" else None},
                "segments": segments,
                "metadata": {"created_at": now, "processed_at": now, "provider": provider,
                             "model": model, "segment_count": len(segments),
                             "speakers": {str(i): p["name"] for i, p in enumerate(v.vcon_dict["parties"])}},
            }, ensure_ascii=False),
        )
        self._ext("wtf_transcription")

        v.add_attachment(purpose="case_metadata", encoding="json", body=json.dumps({
            "docket_number": docket,
            "additional_docket_numbers": case.get("additional_docket_numbers"),
            "term": case.get("term"),
            "first_party": case.get("first_party"), "first_party_label": case.get("first_party_label"),
            "second_party": case.get("second_party"), "second_party_label": case.get("second_party_label"),
            "manner_of_jurisdiction": case.get("manner_of_jurisdiction"),
            "lower_court": (case.get("lower_court") or {}).get("name"),
            "argued": argued_dates(case),
            "question": case.get("question"),
            "facts_of_the_case": case.get("facts_of_the_case"),
            "conclusion": case.get("conclusion"),
            "citation": case.get("citation"),
            "oyez_url": case.get("href"),
            "justia_url": case.get("justia_url"),
        }, ensure_ascii=False))

        v.add_attachment(purpose="tags", encoding="json", body=json.dumps({
            "source": "oyez", "court": "scotus", "term": str(case.get("term")), "docket": docket,
        }))

        self._lawful_basis(now)
        v.add_attachment(purpose="ingress_info", encoding="json", body=json.dumps({
            "source": "scotus2vcon", "converter_version": __version__, "converted_at": now,
            "oyez_case": case.get("href"), "oyez_audio": audio.get("href") or f"{OYEZ}/case_media/oral_argument_audio/{audio.get('id')}",
        }))

        created = v.vcon_dict.get("created_at")
        for a in v.vcon_dict.get("attachments") or []:
            a.setdefault("party", 0)
            a.setdefault("dialog", 0)
            a.setdefault("start", created)
        v.vcon_dict["updated_at"] = now
        return v

    def _lawful_basis(self, now: str) -> None:
        v = self.vcon
        v.add_lawful_basis_attachment(
            lawful_basis="legitimate_interests",
            expiration=None,
            purpose_grants=[{"purpose": p, "granted": True, "granted_at": now}
                            for p in ("recording", "transcription", "publication", "archival", "analysis")],
            terms_of_service="https://www.supremecourt.gov/oral_arguments/argument_audio.aspx",
            metadata={
                "terms_of_service_name": "Supreme Court of the United States, Argument Audio",
                "jurisdiction": "US",
                "controller": "Supreme Court of the United States",
                "notes": ("Oral argument audio and transcripts are public records the Court itself "
                          "records and publishes. Oyez (Cornell LII / Justia / Chicago-Kent) redistributes them."),
            },
        )
        att = v.vcon_dict["attachments"][-1]
        body = att.get("body")
        if isinstance(body, str):
            body = json.loads(body)
        body["proof_mechanisms"] = [
            {"mechanism_type": "external_system",
             "description": "Supreme Court of the United States argument audio archive, "
                            "https://www.supremecourt.gov/oral_arguments/argument_audio.aspx"},
            {"mechanism_type": "external_system",
             "description": "Oyez Project case record and media, https://api.oyez.org"},
        ]
        att["body"] = json.dumps(body)
        att["encoding"] = "json"
        self._ext("lawful_basis")


# ---------------------------------------------------------------- driver

def convert_case(client: Oyez, case: dict, out_dir: Path, args) -> int:
    """Write one vCon per oral argument recording. Returns count written."""
    written = 0
    for audio_ref in case.get("oral_argument_audio") or []:
        if audio_ref.get("unavailable"):
            continue
        term, docket = case.get("term"), case.get("docket_number")
        media_id = audio_ref.get("id")
        safe_docket = re.sub(r"\s+", "-", str(docket).strip())  # "1 MISC", "23-108 "
        out = out_dir / str(term) / f"{safe_docket}_{media_id}.vcon.json"
        if out.exists() and not args.force:
            log.debug("exists, skipping %s", out)
            continue
        audio = client.argument_audio(media_id)
        if not audio or audio.get("unavailable"):
            continue
        files = [m for m in audio.get("media_file") or [] if m]  # nulls here too
        media = next((m for m in files if m.get("mime") == "audio/mpeg"), None) or next(iter(files), None)
        if not media or not media.get("href"):
            log.warning("no media file for %s/%s media %s", term, docket, media_id)
            continue

        av = ArgumentVcon()
        for court in case.get("heard_by") or []:
            for j in (court or {}).get("members") or []:  # heard_by can hold nulls
                av.party(j, "justice")
        for a in case.get("advocates") or []:
            if a:  # advocates can hold nulls too
                av.party(a.get("advocate"), "advocate", a.get("advocate_description"))

        content_hash = None
        audio_path = HERE / "downloads" / "audio" / str(term) / Path(media["href"]).name
        need_audio = args.hash_audio
        transcript_json = audio.get("transcript")
        if not transcript_json or not transcript_json.get("sections"):
            need_audio = True
        if need_audio:
            try:
                content_hash = content_hash_token(client.download(media["href"], audio_path))
            except httpx.HTTPError as e:
                log.warning("audio download failed for %s: %s", media["href"], e)
                if not transcript_json:
                    continue

        if transcript_json and transcript_json.get("sections"):
            def party_of(speaker: dict) -> int | None:
                return av.party(speaker, "speaker")
            segments, text, duration = segments_from_oyez(transcript_json, party_of)
            provider, model, language = "oyez", "court-transcript", "en"
        else:
            if args.no_whisper:
                log.info("no transcript and --no-whisper: %s/%s", term, docket)
                continue
            local = transcribe_local(audio_path, args.whisper_model)
            if not local:
                continue
            segments, text, duration, language = local
            provider, model = "mlx-whisper", args.whisper_model

        v = av.build(case, audio, media, (segments, text, duration), provider, model, content_hash, language)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(v.to_json())
        if not args.keep_audio and audio_path.exists():
            audio_path.unlink()
        written += 1
        log.info("wrote %s (%s, %d segments)", out.relative_to(HERE), provider, len(segments))
    return written


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--term", type=int, help="first (or only) term to convert")
    p.add_argument("--term-end", type=int, help="last term, inclusive")
    p.add_argument("--docket", help="one case as TERM/DOCKET, e.g. 2023/22-451")
    p.add_argument("--output", type=Path, default=HERE / "output")
    p.add_argument("--force", action="store_true", help="regenerate existing vCons")
    p.add_argument("--hash-audio", action="store_true", help="download every MP3 to set content_hash")
    p.add_argument("--keep-audio", action="store_true", help="keep downloaded MP3s under downloads/audio")
    p.add_argument("--no-whisper", action="store_true", help="skip cases Oyez has no transcript for")
    p.add_argument("--whisper-model", default="mlx-community/whisper-turbo")
    p.add_argument("--limit", type=int, help="stop after N vCons (smoke tests)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("vcon").setLevel(logging.WARNING)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("vcon"):
            logging.getLogger(name).setLevel(logging.WARNING)

    client = Oyez(HERE / "downloads" / "oyez")
    total = 0
    if args.docket:
        term, docket = args.docket.split("/", 1)
        case = client.case(term, docket)
        if not case:
            log.error("case not found: %s", args.docket)
            return 1
        return 0 if convert_case(client, case, args.output, args) else 1

    if args.term is None:
        p.error("--term or --docket required")
    for term in range(args.term, (args.term_end or args.term) + 1):
        cases = client.cases_for_term(term)
        log.info("term %d: %d cases", term, len(cases))
        for stub in cases:
            case = client.case(term, stub["docket_number"], stub.get("ID"), stub.get("href")) if stub.get("docket_number") else None
            if not case:
                continue
            try:
                total += convert_case(client, case, args.output, args)
            except Exception:  # one bad case must not end the run
                log.exception("failed %s/%s", term, stub.get("docket_number"))
            if args.limit and total >= args.limit:
                log.info("limit reached: %d", total)
                return 0
    log.info("done: %d vCons written", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
