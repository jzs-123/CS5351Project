# R3 verification record

## Baseline and offline results

Base commit342b6f5d66c3f0ae93ef2aa6e2b64c00c3eff9ff already includes R1/R2/R4/R5. P2 final1606 passed; P3 1680; P4 1730; P5 1768; final P6 compatibility fix1774 passed,6 skipped,2 deselected,24 warnings,100 subtests,55.10s. Skips are Windows POSIX behavior and optional Bedrock; real integration cases are default-excluded. Ruff and git diff --check passed. The17-file final hash manifest is included; original P5 fifteen files were unchanged during P6.

## Real application runs

| Evidence | Actual observation |
|---|---|
| probe-01 | school endpoint returnedOK;1 call,20 input/1 output tokens |
| normal-01 | original flow,11 calls,Hold and15% target; configured2048 but some reported outputs exceeded it; retained as pre-fix evidence |
| limit-01 | after subclass fix, native max_tokens64; response64 tokens, finish_reason=length |
| normal-02 | post-fix original flow, AAPL2024-05-10, fake current15%, Overweight→20%, positive target stop policy8%, confidence null |
| normal-02 usage | 11 calls,48554 input/14128 output tokens, max reported output2467≤4096; R3 before/after call counts11/11 |
| historical inputs | 21 closes,20 simple returns, independently computed daily stdev0.01860240347509025; selected snapshot/digest recorded |
| degradation-02 | controlled data-unavailable injection reusing normal-02 analysis; unchanged rating/decision, needs_data and null target;0 new model calls |
| budget-01 | real configured graph stopped by R5 before model dispatch;0 completed calls, cost JSON persisted, no R3 success hook |

The two full analyses gave different ratings. Both are preserved; model nondeterminism is not hidden or "fixed" by changing the rating. P6 application calls total24(probe1+first11+limit1+post-fix11), input107079/output28255 tokens; OpenCode development calls are recorded separately, not included in these app counters.

The school deployment is not in the price table: unknown_models and known=false are explicit. A recorded numeric USD0 is an unpriced estimate, NOT evidence of free service or an accurate dollar hard cap. Request/input/output controls were additional operational guards. The first Yahoo crumb attempt warned about SSL then continued successfully; no TLS verification was disabled.

## Compatibility correction

Offline payload inspection showed the dependency emitted max_completion_tokens. The DeepSeek subclass now restores max_tokens, including per-call precedence, without changing other providers or reasoning-content roundtrip. [DeepSeek's primary API reference](https://api-docs.deepseek.com/api/create-chat-completion/) documents max_tokens. The school's internal handling of the previous field was not inspected; the pre-fix difference and post-fix64-token behavior are observed facts.

## Presentation and limits

JSON, Markdown, HTML and terminal use recorded values and tested transitions. Browser screenshot inspection was not completed: headless rendering failed, then the in-app browser explicitly rejected local file URLs. That path was stopped; static content/escaping/scoped CSS checks are not presented as visual inspection. No broker orders, real account or profit validation occurred. This is a constrained software smoke, not an investment recommendation or broad quality evaluation.
