# aitest∩╝êAI Σ╗úτÉåµ╕¼Φ⌐ªσ░êσ«╢∩╝ëΓÇö τñ║µäÅσ£û V2 Σ║ñΣ╗╢Φ¬¬µÿÄ

- σêåµö»∩╝Ü`impl/aitest`∩╝êworktree∩╝Ü`C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\aitest`∩╝ë
- base commit∩╝Ü`bea2e5c`∩╝êgrok/integrate∩╝îπÇîround4 #2: T4 prompt 'current' = live app promptπÇì∩╝ë
- τ¿ïσ╝Å commit∩╝Ü
  - `9d562b02ceab43ff1f94f56dc5de4eb54c264a52` feat(visual): V2 visual_routes router + independent visual channel + viewer page
  - `6da10c7b0dc72081ad13549f892437cebebdc1ca` feat(visual): viewer forwards ?k= listen key to /ws/visual for ws_guard
- µ£¼ NOTES.md σÅªΣ╗ÑΣ╕ÇσÇï commit µÅÉΣ║ñσ£¿Σ╕èΦ┐░σà⌐σÇï commit Σ╣ïσ╛î∩╝ê`git log -1 impl/aitest` σì│µÿ»∩╝ëπÇé
- V1 Σ╛åµ║É∩╝êσö»Φ«ÇΦñçΦú╜∩╝ë∩╝Ü`zen-bridge-visual-v1` σêåµö» `grok-bot/visual-v1` commit `721b2f0` τÜä `sidecar/live-room/app/visual.py`πÇéσà⌐µú╡µ¿╣Θâ╜µ▓Æµ£ë `docs/VISUAL.md`∩╝îµëÇΣ╗ÑΣ╗ïΘ¥óΣ╛¥ V1 µ¿íτ╡äµ£¼Φ║½∩╝ê`visual_draft` schemaπÇü`VisualLLM.complete(messages, *, timeout_s)`∩╝ëπÇé

## σüÜΣ║åΣ╗ÇΘ║╝

µû░σó₧µ¬öµíê∩╝êσà¿Θâ¿σ£¿ `sidecar/live-room/`∩╝îµ▓Æµ£ëµö╣Σ╗╗Σ╜òµùóµ£ëµ¬öµíê∩╝ë∩╝Ü

| µ¬öµíê | σàºσ«╣ |
|---|---|
| `app/visual.py` | σ╛₧ V1 τº╗µñìπÇéV2 µû░σó₧∩╝Ü`strip_think()`∩╝êσÄ╗µÄë `<think>ΓÇª</think>`πÇüµ£¬ΘûëσÉêτÜä `<think>`πÇüσÅ¬µ£ëτ╡Éσ░╛τÜä `</think>`∩╝ë∩╝¢σÄ╗Θçìµö╣τö¿**µ¡úΦªÅσîûΘ¢£µ╣è**∩╝êNFKC∩╝ïcasefold∩╝îσÄ╗µÄëτ⌐║τÖ╜∩╝Åµ¿ÖΘ╗₧∩╝Åτ¼ªΦÖƒ∩╝ëΣ╕öσÅ¬σ£¿ `dedupe_window_ms`∩╝êΘáÉΦ¿¡ 10 σêåΘÉÿ∩╝îτÆ░σóâΦ«èµò╕ `BREEZE_VISUAL_DEDUPE_WINDOW_MS`∩╝ëσàºµ£ëµòê∩╝¢`is_presentable()`∩╝êτ⌐║σìíπÇüσÅ¬µ£ëΣ╜öΣ╜ìµ¿ÖΘíîπÇüµ«ÿτòÖ think µ¿Öτ▒ñΣ╕Çσ╛ïΣ╕ìσ╗úµÆ¡∩╝ë∩╝¢`build_llm_from_env()` µö╣τé║**σÅ¬µÄÑσÅù loopback**∩╝ê127.x / localhost / ::1∩╝ë∩╝îΘ¥₧µ£¼µ⌐ƒ URL Σ╕Çσ╛ïσü£τö¿∩╝îΣ╕ìµ£âµèèσ¡ùσ╣òΘÇüσç║τ¡åΘ¢╗πÇé |
| `app/visual_routes.py` | `VisualSubscriber`∩╝êµ»ÅΣ╜ìΦºÇτ£ïΦÇàµ£ëτòîΣ╜çσêùπÇüµ╗┐Σ║åΣ╕ƒµ£ÇΦêè∩╝ëπÇü`VisualChannel`∩╝êτì¿τ½ïτÜäµ»Åµê┐Φ¿éΘû▒ΦÇàΘ¢åσÉêΦêçΘçìµÆ¡µ¡╖σÅ▓∩╝¢σÅ¬µö╢ `visual_draft` / `visual_hello`∩╝îσ¡ùσ╣òΣ║ïΣ╗╢Σ╕ƒΘÇ▓Σ╛åµ£â `ValueError`∩╝ëπÇü`VisualHub`∩╝êµ»Åµê┐Σ╕ÇσÇï `VisualGenerator`∩╝ÜΦº╕τÖ╝πÇüµ»Åµê┐σå╖σì╗πÇüσÄ╗ΘçìπÇüLLM ΘÇ╛µÖé∩╝ÅΘî»Φ¬ñµö╣τö¿ΦíôΦ¬₧σìí∩╝¢`feed()` σÅ»σ╛₧Σ╗╗Σ╜òσƒ╖Φíîτ╖Æσæ╝σÅ½∩╝îµ£â `call_soon_threadsafe` σ¢₧Σ║ïΣ╗╢Φ┐┤σ£ê∩╝ëπÇü`build_visual_router()`∩╝êAPIRouter∩╝ëπÇé |
| `app/static/visual.html`∩╝ï`visual.js` | τì¿τ½ïΦºÇτ£ïΘáü `/visual?room=<µê┐Θûô>[&k=<listen key>]`∩╝îΘÇú `/ws/visual`∩╝îσì│µÖéΘí»τñ║τñ║µäÅσ£ûσìí∩╝êµ¿ÖΘíîΣ╕¡Φï▒πÇüσàºµûçπÇüΦíôΦ¬₧πÇüMermaid σÄƒσºïτó╝∩╝ëπÇéσà¿Θâ¿τö¿ `textContent`∩╝îΣ╕ìΦºúµ₧Éµ¿íσ₧ïΦ╝╕σç║τé║ HTMLπÇé`visual.js` µÿ» ES module∩╝êµ£ë `export`∩╝îτ¼ªσÉê `test_static_cache.py` σ░ì `static/*.js` τÜäΦªüµ▒é∩╝ëπÇé |
| `tests/test_visual_routes.py` | 30 σÇïµ╕¼Φ⌐ª∩╝êσüç LLMπÇüσüçµÖéΘÉÿ∩╝îτäíτ╢▓Φ╖»πÇüτäíµ¿íσ₧ï∩╝ëπÇé |

Router τ½»Θ╗₧∩╝Ü

- `GET /visual` ΦºÇτ£ïΘáüπÇü`GET /visual/visual.js`
- `GET /api/visual/{room_id}`∩╝Ü`{room_id, enabled, subscribers, skipped, dropped_jobs, history}`
- `POST /api/visual/{room_id}/trigger[?force=1]`∩╝ÜΣ╕╗µîüΣ║║µëïσïòΦº╕τÖ╝∩╝¢ΘáÉΦ¿¡ `host_guard` σÅ¬σàüΦ¿▒µ£¼µ⌐ƒ∩╝êloopback∩╝ëτö¿µê╢τ½»
- `WS /ws/visual?room_id=...[&k=...]`∩╝ÜσàêΘÇü `{"type":"visual_hello","room_id","enabled","history":[...]}`∩╝îΣ╣ïσ╛îµ»Åσ╝╡µû░σìíΘÇüΣ╕Çσëç `visual_draft`∩╝êV1 schema∩╝ëπÇéΦêç `/ws/listen` σ«îσà¿σêåΘûïπÇé

## µ╕¼Φ⌐ªµîçΣ╗ñΦêçτ╡Éµ₧£

```powershell
cd C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\aitest\sidecar\live-room
C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_visual_routes.py -q -p no:cacheprovider
```

τ╡Éµ₧£∩╝êτ¡åΘ¢╗ DESKTOP-P8RGA3A σ»ªµ╕¼∩╝î2026-10-10 23:39 σÅ░σîùµÖéΘûôσëìσ╛î∩╝ë∩╝Ü**30 passed, 0 failed, 1 warning∩╝î1.23ΓÇô1.29 s**∩╝¢µ£¼µ¼íΘûïτÖ╝σà▒Φ╖æ 6 µ¼í∩╝îµ£Çσ╛îσà⌐µ¼í∩╝êµ£Çτ╡éτ¿ïσ╝Åτó╝∩╝ëΦêçσàêσëì 4 µ¼íτÜå 30 passed∩╝êµ¬óµƒÑΘ¥₧σü╢τÖ╝∩╝ëπÇéwarning µÿ» starlette τÜä `httpx` µúäτö¿µÅÉτñ║∩╝îΦêçµ£¼µ¿íτ╡äτäíΘù£πÇéΣ╛¥ΦªÅσ«ÜσÅ¬Φ╖æµ£¼µ¬ö∩╝î**µ▓Æµ£ëΦ╖æσà¿σÑù**πÇé

µ╢╡Φôï∩╝ÜΦº╕τÖ╝∩╝êσ«Üτ¿┐µ╗┐ 30 s µëìΦº╕τÖ╝πÇüΘ¥₧ final∩╝Åσú₧Σ║ïΣ╗╢σ┐╜τòÑ∩╝ëπÇüσå╖σì╗∩╝êσüçµÖéΘÉÿσëìΘÇ▓σëìσ╛î∩╝ëπÇüµ¡úΦªÅσîûσÄ╗ΘçìΦêçµÖéΘûôτ¬ùΘüÄµ£ƒπÇüµëïσïò forceπÇüµ»Åµê┐σå╖σì╗∩╝ïµê┐ΘûôΘÜöΘ¢óπÇüσú₧µê┐ΦÖƒπÇüσü£τö¿µÖéΣ╕ìσïòΣ╜£πÇüΘá╗ΘüôσêåΘ¢ó∩╝êσ¡ùσ╣òσ₧ïσêÑΦó½µïÆπÇürouter µ▓Æµ£ë `/ws/listen`∩╝ëπÇüµàóΦ¿éΘû▒ΦÇàΣ╕ƒµ£ÇΦêèΣ╕öΣ╕ìµïûτ┤»σ┐½Φ¿éΘû▒ΦÇàπÇüτÖ╝Σ╜êσàºσ«╣µÿ»τì¿τ½ïσë»µ£¼πÇüLLM Σ╛ïσñûΓåÆΦíôΦ¬₧σìíπÇüLLM ΘÇ╛µÖé∩╝ê0.05 s∩╝ëΓåÆΦíôΦ¬₧σìíπÇüτäíΦíôΦ¬₧µÖéσñ▒µòùΣ╕ìσ╗úµÆ¡πÇü6 τ¿«σ₧âσ£╛Φ╝╕σç║Σ╕ìσ╗úµÆ¡πÇüthink σë¥ΘÖñ∩╝êσÉ½ code fenceπÇüµ¼äΣ╜ìσàº think∩╝ëπÇüΣ╕ìσ«ëσà¿ Mermaid ΘÖìτ┤ÜπÇüΦ╖¿σƒ╖Φíîτ╖Æ feedπÇüΘáÉΦ¿¡ LLM σÅ¬ΘÖÉµ£¼µ⌐ƒΣ╕öΘáÉΦ¿¡Θù£ΘûëπÇüΦºÇτ£ïΘáü∩╝ÅJS µ£ëΣ╛¢µçëπÇüWebSocket hello∩╝ïσì│µÖéσìí∩╝ïµê┐ΘûôΘÜöΘ¢ó∩╝ïµÖÜσê░ΘçìµÆ¡πÇüws_guard µïÆτ╡òπÇüµëïσïòΦº╕τÖ╝τ½»Θ╗₧ΦêçΘáÉΦ¿¡µ£¼µ⌐ƒΘÖÉσ«ÜπÇé

## µÄ¢Φ╝ëµû╣σ╝Å∩╝êτö▒µò┤σÉêΦÇàµö╣ server.py∩╝¢µêæµ▓Æµ£ëσïò server.py / pipeline.py∩╝ë

σ£¿ `app/server.py` τÜä `create_app()` σàº∩╝Ü

1. µ¬öΘá¡ import∩╝Ü
   ```python
   from app.visual_routes import VisualHub, build_visual_router
   ```
2. `bus = RoomBus(...)`∩╝êτ┤äτ¼¼ 882 Φíî∩╝ëΣ╣ïσ╛îσ╗║τ½ï hub∩╝Ü
   ```python
   visual_hub = VisualHub.from_env(glossary_provider=<σ¢₧σé│Φ⌐▓µê┐ΦíôΦ¬₧ rows τÜäσç╜σ╝Å∩╝îσÅ»τé║ async∩╝¢µ▓Æµ£ëσ░▒τ£üτòÑ>)
   ```
   µ▓Æµ£ëΦ¿¡σ«Ü `BREEZE_VISUAL_LLM_BASE_URL`∩╝ï`BREEZE_VISUAL_LLM_MODEL` µÖé hub µÿ»σü£τö¿τÜä∩╝êΘáüΘ¥óΘí»τñ║πÇîτñ║µäÅσ£ûµ£¬σòƒτö¿πÇì∩╝ë∩╝îΣ╕ìσ╜▒Θƒ┐σ¡ùσ╣òπÇé
3. `app.mount("/static", ...)`∩╝êτ┤äτ¼¼ 1290 Φíî∩╝ëΣ╣ïσ╛î∩╝Ü
   ```python
   app.state.visual_hub = visual_hub
   app.include_router(build_visual_router(visual_hub, ws_guard=_visual_ws_guard))
   ```
   σ╗║Φ¡░τÜä `_visual_ws_guard`∩╝êµö╛σ£¿ `/ws/listen` σ«Üτ╛⌐ΘÖäΦ┐æ∩╝îΘçìτö¿µùóµ£ëµ¬óµƒÑ∩╝ë∩╝Ü
   ```python
   def _visual_ws_guard(ws, room_id):
       if not audience_origin_allowed(ws.headers.get("origin"), ws.headers.get("host", ""), settings, _audience_extra_hosts()):
           return False
       room = book.get(room_id)
       return room is None or _listener_authorized(ws, room, ws.query_params.get("k", ""))
   ```
   ∩╝êΣ╕ìσé│ `ws_guard` Σ╣ƒΦâ╜Φ╖æ∩╝îΣ╜åσ░▒µ▓Æµ£ë origin∩╝Ålisten key µ¬óµƒÑπÇé∩╝ë
4. **Φº╕τÖ╝Θ╗₧**∩╝Üσ£¿ `on_event()`∩╝êτ┤äτ¼¼ 1083 Φíî∩╝ë`_fanout(snap)` Σ╣ïσ╛îσèáΣ╕ÇΦíî∩╝Ü
   ```python
   visual_hub.feed(snap)
   ```
   `feed()` σÅ¬τ£ï `type == "final"`πÇüσÉêµ│ò room_idπÇüµ£ë zh Φêç t0/t1 τÜäΣ║ïΣ╗╢∩╝îσà╢Θñÿτ¢┤µÄÑσ¢₧ False∩╝¢σ«âΣ╕ìσüÜ I/OπÇüΣ╕ìτ¡ëσ╛à∩╝îσÅ»σ╛₧Σ╗╗Σ╜òσƒ╖Φíîτ╖Æσæ╝σÅ½πÇéµëÇΣ╗Ñ**Σ╕ìΘ£ÇΦªüµö╣ pipeline.py**ΓÇöΓÇöpipeline τÜäσ«Üτ¿┐∩╝ê`Segment.public()` τÜä `type: "final"`∩╝ëµ£¼Σ╛åσ░▒τ╢ôΘüÄ `on_event` ΓåÆ `bus.publish`πÇéΦïÑµò┤σÉêΦÇàσüÅσÑ╜σ£¿ pipeline τ½»σæ╝σÅ½∩╝îΣ╜ìτ╜«µÿ» pipeline τöóτöƒ `status in {"ready","silent"}` τÜä Segment Σ╕ªΘÇüσç║ `public()` τÜäσ£░µû╣∩╝îσæ╝σÅ½σÉîΣ╕ÇσÇï `visual_hub.feed(segment.public())`πÇé
5. µê┐Θûôτ╡Éµ¥ƒ∩╝Üσ£¿ `bus.retire(room_id)` / `bus.drop(room_id)`∩╝êτ┤äτ¼¼ 1118πÇü1127 Φíî∩╝ëµùü `await visual_hub.close_room(room_id)`∩╝êΦïÑΦ⌐▓ΦÖòΣ╕ìµÿ» async∩╝îτö¿ `asyncio.get_running_loop().create_task(...)`∩╝ëπÇé
6. Θù£µ⌐ƒ∩╝Üσ£¿ lifespan τÜä `shutdown()` σàº `await visual_hub.stop()`πÇé

µ£¼µ⌐ƒµ¿íσ₧ïτ»äΣ╛ï∩╝êσÅ¬Φ¿¡τÆ░σóâΦ«èµò╕∩╝îΣ╕ìµö╣ Ollama Φ¿¡σ«Ü∩╝ë∩╝Ü`BREEZE_VISUAL_LLM_BASE_URL=http://127.0.0.1:11434/v1`πÇü`BREEZE_VISUAL_LLM_MODEL=<σ╖▓σ«ëΦú¥τÜäµ£¼µ⌐ƒµ¿íσ₧ïσÉì>`πÇé

τ£ïσ╛ùσê░τÜäΦ«èσîû∩╝ÜµÄ¢Σ╕èσ╛îΘûï `http://127.0.0.1:<port>/visual?room=class`∩╝îσ░▒Φâ╜τ£ïσê░τñ║µäÅσ£ûΘá╗ΘüôτÜäσì│µÖéτïÇµàïΦêçσìíτëçπÇé

## σÅâµò╕Φêçµò╕σ¡ùΣ╛åµ║É

- Φº╕τÖ╝τ¬ù 30ΓÇô60 sπÇüσå╖σì╗ 30 sπÇüLLM ΘÇ╛µÖé 15 sπÇüµ»Åµê┐σ╖ÑΣ╜£Σ╜çσêù 2πÇüµ£ÇσñÜ 12 ΦíîπÇüΦíôΦ¬₧ 24 σÇï∩╝Üµ▓┐τö¿ V1 `app/visual.py`∩╝êcommit 721b2f0∩╝ëΘáÉΦ¿¡σÇ╝∩╝îµ£¬σ£¿τ¡åΘ¢╗σüÜµòêΦâ╜ΘçÅµ╕¼πÇé
- σÄ╗ΘçìµÖéΘûôτ¬ù 10 σêåΘÉÿπÇüµ»ÅΦºÇτ£ïΦÇàΣ╜çσêù 8 σëçπÇüΘçìµÆ¡µ¡╖σÅ▓ 5 σ╝╡πÇüµ»Åµê┐µ£ÇσñÜ 64 Σ╜ìΦºÇτ£ïΦÇà∩╝Üµ£¼µ¼íΦ¿¡Φ¿êΘü╕σÇ╝∩╝îµ£¬ΘçÅµ╕¼πÇé
- µ╕¼Φ⌐ªΦÇùµÖé 1.23ΓÇô1.29 s∩╝Üτ¡åΘ¢╗σ»ªµ╕¼πÇé

## σ╖▓τƒÑΘÖÉσê╢

- Mermaid σÅ¬Θí»τñ║σÄƒσºïτó╝µûçσ¡ù∩╝îµ▓Æµ£ëµ╕▓µƒôµêÉσ£û∩╝êΣ╕ìσ╝òσàÑσñûΘâ¿ JS σç╜σ╝Åσ║½∩╝¢Φªüµ╕▓µƒôΘ£Çµèè mermaid µö╛ΘÇ▓ vendor Σ╕ªσÅªσ»½ CSP σ«ëσà¿τÜäµ╕▓µƒô∩╝ëπÇé
- ΦºÇτ£ïΘáüµ▓Æµ£ëΘÇúσê░ host.html∩╝Åroom.html∩╝êΘéúΣ║¢µ¬öµíêτªüµ¡óΣ┐«µö╣∩╝ë∩╝¢Θ£Çµò┤σÉêΦÇàσÅªσèáΘÇúτ╡Éµêûτ¢┤µÄÑΘûï `/visual`πÇé
- `VisualHub` τ╢üσ«Üτ¼¼Σ╕ÇσÇïσæ╝σÅ½σ«âτÜäΣ║ïΣ╗╢Φ┐┤σ£ê∩╝¢σ£¿ uvicorn σû«Σ╕ÇΦ┐┤σ£êΣ╕ïµ▓ÆσòÅΘíîπÇéΦïÑ `close_room` µ▓ÆµÄÑ∩╝îµê┐ΘûôτÜä generator µ£âτòÖσê░Θù£µ⌐ƒπÇé
- ΘáÉΦ¿¡ `host_guard` σÅ¬Φ¬ì loopback τö¿µê╢τ½»∩╝¢ΦïÑΣ╕╗µîüΣ║║σ╛₧σêÑσÅ░Φú¥τ╜«µôìΣ╜£∩╝îΘ£Çσé│σàÑΣ╜┐τö¿Σ╕╗µîüΣ║║ token τÜä guardπÇé
- µ▓ÆΦ╖æσà¿σÑùµ╕¼Φ⌐ª∩╝êΣ╛¥τ¡åΘ¢╗Φ¿ÿµå╢Θ½öΦªÅσ«Ü∩╝ëπÇé`test_static_cache.py` µ£âµÄâ `static/*.js` Φªüµ▒éσÉ½ `export ` Σ╕öσÅ»Θçìµû░Θ⌐ùΦ¡ë∩╝¢`visual.js` σ╖▓µÿ» ES module∩╝îµçëσÅ»ΘÇÜΘüÄ∩╝îΣ╜åΘ£Çµò┤σÉêΦÇàΦ╖æσà¿σÑùτó║Φ¬ìπÇé
- σü£τö¿τïÇµàïΣ╕ï `/api/visual/...` Φêç `/ws/visual` Σ╗ìσÅ»ΘÇú∩╝îσÅ¬µÿ» `enabled: false`πÇüΣ╕ìµ£âµ£ëσìíτëçπÇé

## BLOCKED

τäíπÇé
