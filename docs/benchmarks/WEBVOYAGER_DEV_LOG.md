# Emergence WebVoyager: dev log

**Tasks:** `benchmarks/webvoyager/upstream/webvoyager_template.json`, 535 tasks.
- Source: commit `e988bb1` of EmergenceAI/EmergenceWebVoyager (MIT).
- All 535 instantiated safely, with 0 exclusions (see `upstream/SOURCE.md`).

**Split:** fixed before any run (`benchmarks/webvoyager/splits.json`): 30 dev tasks and 45 test tasks, 3 per site.

Dev results are for finding weaknesses. They are not judged and are not a score.

## Dev batch r1 (2026-10-23): 5 tasks before the daily quota ran out
| Task | End | What happened |
|---|---|---|
| allrecipes-016 | ADVI's own step limit | Repeated identical searches. One snapshot returned "0 interactive elements" just after load, then a click on a stale ref `e0` failed |
| amazon-051 | ADVI's own step limit | Navigation stuck |
| amazon-067 | answered | Searched and read the results, but the answer was replaced by "couldn't verify the final state: Extracted 3060 characters" |
| apple-077 | ADVI's own step limit | Navigation stuck; Apple has no usable site search box |
| apple-090 | needs_user | The "Buy iMac" **link** (it opens the price page) was gated as a purchase |
| arxiv-119 | quota | Daily quota; this task will be rerun |

## Fixed (general fixes, each with tests in `tests/current/test_benchmark_dev_fixes.py`)
- **Browsing answers stand.** A turn made only of searching, navigating, link clicks and reading keeps the AI's answer, even after a harmless miss such as no search box. Typing into pages and failed final steps are still reported.
- **Shop and booking *links* browse.** "Buy iMac", "Book now" and "Shop" links open pages. Pay, place order, confirm, send, submit and delete stay gated even on links.
- **Short rate limits are waited out** by ADVI itself (25 s or less), with a spoken notice.
- **Quieter console:** the "Could not find in-site search input" warning now logs at INFO.

## Fixed in round 2 (2026-10-23)
- **Empty snapshot right after a page loads:** `inspect_web_page` waits for the page to settle and looks once more when it finds 0 elements.
- **Repeated searches:** a search this task already ran is not run again. Ignoring the site's name, "site:" and word order, ADVI points the AI at the open results page instead.
- **ADVI's internal step limit** is unchanged. Wasted steps were the problem, not the limit.
