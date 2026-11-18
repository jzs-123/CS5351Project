# R3 defect diary

| Stage | Symptom / reproduction | Root issue and correction | Exposed contract |
|---|---|---|---|
| P2 | zero-cap result still had8% stop | zero final target must null stop | zero target has no stop |
| P3 | snapshot could appear written after replace failure | verify actual persisted content | evidence must not invent success |
| P3 | local midnight could accept an unsettled UTC day | require date strictly before both days | no partial future candle |
| P4 | context file/config changed after start affected result | freeze detached prepared values before signature/run | one run one input |
| P4 | False==0/True==1 hid invalid in-memory change | validate current mapping before path refresh | explicit bad value rejected |
| P4 | default-off changed checkpoint key | leave inert legacy config unchanged at prepare | compatibility |
| P5 | malformed large weight renderedinf% asComputed | bound/safely format executable fractions | no fake executable value |
| P5 | missing history saidnotrequired; zero retained stop | status-aware rendering and stale suppression | report meaning matches result |
| P5 | new tests asserted wrong capoverride and fixed s6 ID | correct tests against math/actual populated sections | tests can also be wrong |
| P6 | actual output exceeded configured cap | DeepSeek wire-key compatibility correction | configured resource limit must reach API |

These were identified in Codex AI review/verification and corrected by OpenCode. They are not represented as defects personally discovered by the student or as independently approved peer reviews.
