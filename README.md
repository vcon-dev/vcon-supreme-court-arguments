# vcon-supreme-court-arguments

Every Supreme Court of the United States oral argument with public audio, as a
vCon. 8,503 recordings across the 1955 through 2025 terms, one file per
argument session under `output/<term>/<docket>_<oyez-media-id>.vcon.json`.

Source is the [Oyez API](https://api.oyez.org): the Court's own argument audio
(MP3) plus speaker-attributed, timestamped transcripts. Where Oyez has audio but
no transcript (196 recordings), the MP3 was transcribed locally with mlx-whisper.

## The corpus

| | |
|---|---|
| vCons | 8,503 |
| Terms | 1955 to 2025 |
| Transcript source | 8,307 Oyez court transcripts, 196 local whisper |
| Audio covered | about 8,600 hours |
| Transcript segments | 4.5 million |
| Parties per vCon | about 11: the sitting justices plus advocates |

`dataset.json` declares the repo as one dataset; its `count` matches the number
of vCon files.

Nine recordings listed by Oyez have no audio file and are not included. Three
cases from the 1950s and 1960s have no oral argument audio on Oyez at all.

## What is in a vCon

- `parties[]`: the justices who heard the case (`role: justice`, title in
  `meta`), the advocates (`role: advocate`, side in `meta.description`), and
  any other transcript speaker (`role: speaker`).
- `dialog[0]`: type `recording`, external MP3 `url`, argument date as `start`,
  duration in seconds. No `content_hash` unless built with `--hash-audio`.
- `analysis[0]`: `wtf_transcription`, vendor `oyez` or `mlx-whisper`, string
  JSON body with `encoding: json`. One segment per Oyez text block, each with
  `speaker` set to the party index. Oyez segments carry confidence 1.0.
- `attachments[]`: `case_metadata` (docket, term, parties, question presented,
  facts, conclusion, citation, Oyez and Justia links), `tags`, `lawful_basis`
  (legitimate interests, Supreme Court public record, proof mechanisms named),
  `ingress_info`.
- `extensions`: `role`, `meta`, `wtf_transcription`, `lawful_basis`.

Follows draft-ietf-vcon-vcon-core-02 with the same conventions as
[ietf2vcon](https://github.com/vcon-dev/ietf2vcon) and its corpus
[ietf-meeting-vcons](https://github.com/vcon-dev/ietf-meeting-vcons).

## Rebuilding or refreshing

```bash
uv venv -p 3.12 && uv pip install vcon httpx mlx-whisper
.venv/bin/python scotus2vcon.py --docket 2023/22-451        # one case
.venv/bin/python scotus2vcon.py --term 2025                 # one term
.venv/bin/python scotus2vcon.py --term 1955 --term-end 2025 # everything
```

Output is idempotent: an existing vCon is left alone unless `--force`, so the
same command doubles as a refresh when new arguments are heard. Oyez responses
are cached under `downloads/oyez/` (not committed).

Options: `--force` regenerate, `--hash-audio` download every MP3 to set
`content_hash` (about 150 GB), `--keep-audio`, `--no-whisper` skip recordings
Oyez has no transcript for, `--whisper-model` (default
`mlx-community/whisper-turbo`), `--limit N`.

mlx-whisper needs Apple Silicon. On other hardware, run with `--no-whisper`
and the 196 whisper-only recordings are skipped.

## Source quirks worth knowing

- Oyez lists sometimes hold nulls: `heard_by`, `advocates`, `media_file`.
- A docket reused within a term is disambiguated in Oyez's case link as
  `406_0`. Use the link from the term listing, not the bare docket.
- `/cases/{term}/{docket}` returns a list when the docket repeats across
  terms; pick by case ID.
- Case metadata text fields carry Oyez's HTML paragraph tags.

## Licence

MIT for the code and the vCon structure. The underlying audio and transcripts
are public records of the Supreme Court of the United States, redistributed by
Oyez (Cornell LII, Justia, Chicago-Kent College of Law).
