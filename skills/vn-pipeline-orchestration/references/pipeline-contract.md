# Pipeline Contract

## Stage Handoffs

| Stage | Requires | Produces | Blocking gate |
| --- | --- | --- | --- |
| Task intake | upload and creation parameters | task ID, raw source, task configuration | upload, ownership, idempotency, and configuration checks |
| Input preflight | uploaded source and limits | preflight report | encoding, size, chapter, and binary checks |
| Source preprocessing | canonical source | normalized chapters and preprocessing checkpoint | source boundary and hash checks |
| Name anonymization | source names and identity policy | name mapping and replacement report | ambiguity and machine-field checks |
| Content warnings | canonical source text and chapter evidence | normalized warning object and checkpoint | schema, digest, and review-policy checks |
| Language detection/translation | source text and target language | translated working text or explicit skip/fallback | language, provenance, and output checks |
| Analysis | source manuscript | context package | preflight and evidence coverage |
| Structure | context package | story tree | DAG and variable validation |
| Branch design | valid tree and states | choices, effects, route plans | effect and compatibility checks |
| Chapter writing | approved node plan | Markdown/JSON artifact | content and state checks |
| Ending design | route history and end states | terminal artifact | causality and reachability |
| Localization | approved text and glossary | locale artifact | IDs, markers, and terminology preserved |
| Character image generation | approved character brief and visual references | character image records and character manifest | identity, state, file, and manifest checks |
| Scene/background/CG image generation | approved scene visual brief and style context | reviewed scene image records | composition, continuity, file, and checkpoint checks |
| Voice generation | approved dialogue, speaker metadata, and locale | reviewed voice records and WAV outputs | text, speaker, locale, timing, and technical checks |
| BGM/SFX/ambience generation | approved sound cue plan | reviewed sound records and WAV outputs | event mapping, coverage, cache, and technical checks |
| Asset integration | reviewed locale and media records plus a supported target contract | scene manifest or builder handoff | all required references resolve |
| Review | any generated artifact | findings and verdict | no blocking findings |
| Build and preview | reviewed story tree, content, and assets | game package and preview endpoint | conversion, asset, and delivery checks |
| Persistence | stage inputs and reviewed artifacts | durable records and checkpoints | all writes succeed |
| Recovery | interrupted stage and persisted checkpoint | verified reuse or targeted regeneration | ownership, hash, and dependency checks |

## Recovery Rules

Stage-level pipeline steps use `pending`, `running`, `completed`, and
`failed`. Node-level checkpoints additionally use `persisting` while the
artifact is between generation and durable publication, and `omitted` when a
node is intentionally excluded from generation. On resume, reuse only verified
completed artifacts (or crash-interrupted artifacts that pass the runtime hash
check). Keep error causes and attempts; do not turn an interrupted node into a
successful one.

## Scope

These Skills are documentation and routing overlays. They do not invoke the
project's runtime by themselves unless a caller explicitly asks for execution
and supplies the required environment. Stage checkpoints are persisted during
execution; the final publication record is written only after its release gate
passes.
