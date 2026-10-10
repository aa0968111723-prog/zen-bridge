# fullstack NOTES∩╝ÜOBS σ¡ùσ╣òτûèσèáσ▒ñ∩╝êzen-bridge τª¬ΘçïµíîΘ¥óτ┐╗Φ¡»∩╝ë

## σüÜΣ║åΣ╗ÇΘ║╝
µû░σó₧ OBS τÇÅΦª╜σÖ¿Σ╛åµ║Éτö¿τÜäΘÇÅµÿÄσ¡ùσ╣òτûèσèáσ▒ñ∩╝ÜσÅ¬Θí»τñ║µ£Çµû░ 2 µ«╡σ¡ùσ╣òπÇüen/ja σ¡ùσ₧ï∩╝êσÉ½Σ╕¡µûçσÄƒµûçσ¡ùσ₧ï∩╝ëπÇüURL σÅâµò╕µÄºσê╢σñûΦºÇπÇüµ▓┐τö¿µùóµ£ëΦºÇτ£╛ WebSocket `/ws/listen` Σ╕ªΦç¬σïòΘÇÇΘü┐ΘçìΘÇúπÇéµ▓Æµ£ë logoπÇüµ▓Æµ£ëσñûΘâ¿ CDNπÇüσÅ¬τö¿ `textContent`πÇé**µ£¬Σ┐«µö╣ server.py µêû BRIEF τªüτó░τÜäΣ╗╗Σ╜òµ¬öµíêπÇé**

- σêåµö»∩╝Ü`impl/fullstack`∩╝êworktree `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\fullstack`∩╝ë
- σƒ║µ║û∩╝Ü`grok/integrate` @ `bea2e5c`∩╝êround4 #2: T4 prompt 'current' = live app prompt∩╝ë
- τ¿ïσ╝Å commit∩╝Ü`727f35d`∩╝êσ«îµò┤ SHA Φªïµûçµ£½∩╝ë∩╝¢µ£¼ NOTES.md σÅªΣ╕ÇσÇï commit
- µû░σó₧µ¬öµíê∩╝êτÜåσ£¿ `sidecar/live-room/` Σ╕ï∩╝îτ┤öµû░σó₧πÇüΣ╕ìµö╣µùóµ£ëµ¬ö∩╝ë∩╝Ü
  - `app/overlay_routes.py`∩╝ÜFastAPI `APIRouter`∩╝î`GET /overlay`πÇü`GET /overlay/{room_id}`
  - `app/static/overlay.html`πÇü`app/static/overlay.js`
  - `tests/test_overlay_routes.py`∩╝êpytest 9 Θáà∩╝ëπÇü`tests/overlay.test.mjs`∩╝êNode `node:test` 17 Θáà∩╝ë

## µ╕¼Φ⌐ªτ╡Éµ₧£
- τ¡åΘ¢╗∩╝êDESKTOP-P8RGA3A∩╝îσà▒τö¿ venv Python 3.12.10∩╝ë∩╝Ü`python -m pytest tests/test_overlay_routes.py -q` ΓåÆ **8 passed, 1 skipped**∩╝êskip = τ¡åΘ¢╗µ▓Æµ£ë node∩╝îNode µ╕¼Φ⌐ªΘáàτ¢«Φ╖│ΘüÄ∩╝ëπÇéΣ╛¥µîçτñ║µ£¬Φ╖æσà¿σÑùπÇé
- box∩╝êXeon∩╝îΘ¥₧ 5600H∩╝¢τ¢╕σÉîµ¬öµíê∩╝îσƒ║µ║ûτé║ box µ▓Öτ¢Æ `4b84597`∩╝ë∩╝Ü`node --test tests/overlay.test.mjs` ΓåÆ **17/17 pass**∩╝êNode 20.19.2 Φêç 22.23.3 Θâ╜ΘüÄ∩╝ë∩╝¢pytest σÉîµ¬ö 9 passedπÇé
- box σà¿σÑù∩╝êσÅâΦÇâτö¿∩╝îσƒ║µ║û `4b84597`∩╝ë∩╝Ü966 passedπÇü24 skipped∩╝¢`test_sim_srt.py`∩╝Å`test_sim_backlog.py` τÜä 100 σêåΘÉÿµ¿íµô¼µÖéΘûôΘûÇµ¬╗σ£¿ box Θ½ÿΦ▓áΦ╝ë∩╝êload avg ~12∩╝ëΣ╕ïσñ▒µòù∩╝îΦêç overlay τäíΘù£∩╝êoverlay τ┤öµû░σó₧µ¬öπÇüµ£¬Φó½σ«âσÇæσî»σàÑ∩╝ë∩╝¢µ£¬Φâ╜σ£¿ box σ«îµêÉσƒ║µ║ûσ░ìτàºσì│Φó½µö╣µ┤╛σê░τ¡åΘ¢╗πÇé
- σÅªσ£¿ box Θ⌐ùΦ¡ë∩╝Üτ£ƒσ»ª `create_app()` µÄ¢Σ╕è router σ╛î∩╝îσ╕╢σÉîµ║É `Origin` ΘÇú `/ws/listen` µ£âΘÇÜΘüÄ origin µ¬óµƒÑ∩╝êµö╢σê░ `room_unavailable`∩╝îΘ¥₧ 1008 µïÆτ╡ò∩╝ëπÇé

## µÄÑτ╖Ü∩╝êµò┤σÉêΦÇà∩╝î`app/server.py` τÜä `create_app()` σàºπÇü`app.mount("/static", ...)` Σ╣ïσ╛î∩╝ë

τûèσèáσ▒ñτ╢▓σ¥Ç∩╝Ü`http://127.0.0.1:<port>/overlay/<µê┐ΘûôΣ╗úΦÖƒ>`∩╝ê`<port>` τé║ app τÜäµ£ìσïÖσƒá∩╝îΦêç host Θáüτ¢╕σÉî∩╝ëπÇé

```python
from app.overlay_routes import router as overlay_router
app.include_router(overlay_router)
```

`overlay.js` τö▒µùóµ£ë `/static` µÄ¢Φ╝ë∩╝ê`RevalidatingStaticFiles`∩╝î`no-cache`∩╝ëµÅÉΣ╛¢∩╝îΣ╕ìΘ£Çσà╢Σ╗ûµÄÑτ╖ÜπÇé
σ¡ùσ╣òΦ╡░µùóµ£ëΦºÇτ£╛τ½» WebSocket `/ws/listen`∩╝êσÉî room.html τÜäσìöσ«Ü∩╝Ü`hello` / caption / `caption_deleted` / `captions_cleared` / `captions_expired` / `ping`ΓåÆ`pong` / `room_unavailable`∩╝ë∩╝î**µ▓Æµ£ëµû░σó₧Φ│çµûÖΦ╖»σ╛æµêûτ½»Θ╗₧**πÇé

## OBS Φ¿¡σ«Ü

Σ╛åµ║É ΓåÆ ∩╝ï ΓåÆ πÇîτÇÅΦª╜σÖ¿πÇìΓåÆ URL σí½ `http://127.0.0.1:<port>/overlay/<µê┐ΘûôΣ╗úΦÖƒ>?lang=en&size=56`∩╝îσ»¼Θ½ÿΦ¿¡µêÉσá┤µÖ»Φºúµ₧Éσ║ª∩╝êσªé 1920├ù1080∩╝ë∩╝îσï╛πÇîΘáüΘ¥óΣ╕ìσÅ»ΦªïµÖéΘù£ΘûëΣ╛åµ║ÉπÇìσÅ»τ£üΦ│çµ║É∩╝¢Σ╕ìΘ£ÇΦªüΦç¬Φ¿é CSS∩╝êΦâîµÖ»µ£¼Φ║½σ░▒µÿ»ΘÇÅµÿÄ∩╝ëπÇé
ΦïÑµê┐Θûôµ£ëΦºÇτ£╛ΘçæΘæ░∩╝îσèáΣ╕è `&k=<listen key>`∩╝êσ░▒µÿ» QR τó╝τ╢▓σ¥ÇΦúí `?k=` τÜäσÇ╝∩╝ëπÇé

## URL σÅâµò╕

µëÇµ£ëσÅâµò╕Θâ╜µ£âΘ⌐ùΦ¡ë∩╝¢Σ╕ìσÉêµ│òσ░▒τö¿ΘáÉΦ¿¡σÇ╝∩╝îµò╕σ¡ùΦ╢àσç║τ»äσ£ìσ░▒σñ╛σê░ΘéèτòîπÇéµƒÑΦ⌐óσ¡ùΣ╕▓**Σ╕ìµ£â**σÅìσ░äσê░ HTML∩╝êΘáüΘ¥óµÿ»Θ¥£µàïµ¬ö∩╝ëπÇé

| σÅâµò╕ | σÇ╝ | ΘáÉΦ¿¡ | Φ¬¬µÿÄ |
|---|---|---|---|
| Φ╖»σ╛æ `/overlay/{room_id}` µêû `room` | `[A-Za-z0-9_-]{1,64}` | `class` | µê┐ΘûôΣ╗úΦÖƒ∩╝¢Φ╖»σ╛æσä¬σàêπÇéΦ╖»σ╛æΣ╕ìσÉêµ│òσ¢₧ 400 |
| `k` | `[A-Za-z0-9_\-.~]{1,256}` | τ⌐║ | ΦºÇτ£╛ΘçæΘæ░∩╝êroom µ£ëΦ¿¡µÖéσ┐àσí½∩╝ë |
| `lang` | `en` \| `ja` | `en` | µ£¼σá┤τ¢«µ¿ÖΦ¬₧Φ¿Ç∩╝îµ▒║σ«Üσ¡ùσ₧ïΦêç `lang` σ▒¼µÇº |
| `show` | `tgt` \| `zh` \| `both` | `tgt` | Θí»τñ║Φ¡»µûç∩╝ÅΣ╕¡µûçσÄƒµûç∩╝Åσà⌐ΦÇà∩╝êσÄƒµûç 0.8em σ£¿Σ╕è∩╝ë |
| `lines` | 1ΓÇô2∩╝êµò┤µò╕∩╝ë | 2 | τò½Θ¥óΣ╕èµ£ÇσñÜσ╣╛µ«╡σ¡ùσ╣ò∩╝îτí¼Σ╕èΘÖÉ 2 |
| `size` | 12ΓÇô160 | 48 | σ¡ùτ┤Ü px |
| `scale` | 0.25ΓÇô4 | 1 | Σ╣ÿσ£¿ `size` Σ╕è∩╝¢µ£Çτ╡éσ¡ùτ┤Üσñ╛σ£¿ 8ΓÇô320 px |
| `pos` | `bottom` \| `top` \| `middle` | `bottom` | σ₧éτ¢┤Σ╜ìτ╜« |
| `align` | `center` \| `left` \| `right` | `center` | µûçσ¡ùσ░ìΘ╜è |
| `color` | `#rgb` / `#rrggbb`∩╝ê`#` σÅ»τ£üπÇüσÅ»σ»½ `%23`∩╝ë | `#ffffff` | σ¡ùΦë▓ |
| `outline` | σÉîΣ╕è | `#000000` | µÅÅΘéèΦë▓ |
| `ow` | 0ΓÇô12 | 3 | µÅÅΘéèσ»¼ px∩╝ê8 µû╣σÉæ text-shadow∩╝ë∩╝î0 = τäíµÅÅΘéè |
| `shadow` | 0 \| 1 | 1 | ΘíìσñûτÜäµƒöσÆîΘÖ░σ╜▒ |
| `margin` | 0ΓÇô400 | 40 | Φ╖¥Σ╕è∩╝ÅΣ╕ïτ╖ú px |
| `maxw` | 30ΓÇô100 | 90 | σ¡ùσ╣òσìÇσ»¼σ║ª∩╝êvw∩╝ë |
| `hide` | 0ΓÇô600 τºÆ | 0 | τäíµû░σ¡ùσ╣òσñÜΣ╣àσ╛îµ╕àτ⌐║τò½Θ¥ó∩╝¢0 = Σ╕ìµ╕à |

σ¡ùσ₧ï∩╝êτäíσñûΘâ¿ CDN∩╝îσà¿τö¿µ£¼µ⌐ƒσ¡ùσ₧ï∩╝ë∩╝Ü
- en∩╝Ü`Segoe UI, Helvetica Neue, Arial, Noto Sans, Liberation Sans, sans-serif`
- ja∩╝Ü`Noto Sans JP, Noto Sans CJK JP, Source Han Sans JP, Yu Gothic UI, Yu Gothic, Meiryo UI, Meiryo, Hiragino Sans, Hiragino Kaku Gothic ProN, MS PGothic, sans-serif`∩╝ê`line-break: strict`∩╝ë
- zh∩╝êσÄƒµûç∩╝ë∩╝Ü`Noto Sans TC, Noto Sans CJK TC, Source Han Sans TC, Microsoft JhengHei UI, Microsoft JhengHei, PingFang TC, Heiti TC, sans-serif`

## Φíîτé║

- σÅ¬Θí»τñ║µ£Çµû░ 2 µ«╡∩╝êΣ╛¥ `session_ord`ΓåÆ`seq`ΓåÆ`cursor` µÄÆσ║Å∩╝ë∩╝¢σÉîΣ╕Çµ«╡Σ╗Ñ `version` Φ╝âµû░ΦÇàΦªåΦôï∩╝¢σ░Üµ£¬τ┐╗σÑ╜µêûτ┐╗Φ¡»σñ▒µòù∩╝ê`status` = missing/error/timeout/cancelled∩╝ëτÜäµ«╡σ£¿ `tgt` µ¿íσ╝ÅΣ╕ìΘí»τñ║∩╝îµëÇΣ╗Ñτò½Θ¥óσü£σ£¿Σ╕èΣ╕Çµ«╡σ«îµêÉτÜäΦ¡»µûçπÇé
- µ»Åµ«╡µ£ÇσñÜτ┤äσà⌐ΦíîΦªûΦª║σêù∩╝¢σ«╣σÖ¿µ║óσç║µÖéσ╛₧Σ╕èµû╣Φúüσêç∩╝îµ£Çµû░µûçσ¡ùΣ╕Çσ«Üσ£¿τò½Θ¥óσàºπÇé
- ΘçìΘÇú∩╝Ü`[0.5,1] ├ù min(30 s, 1 s┬╖2^(n-1))`∩╝îΣ╝║µ£ìσÖ¿τÜä `retry_after_ms` τò╢Σ╕ïΘÖÉ∩╝êµ£ÇσñÜ 60 s∩╝ë∩╝¢`4401`∩╝êΘçæΘæ░σñ▒µòê∩╝ëµ»Å 60 s Φ⌐ªΣ╕Çµ¼í∩╝¢`ended` µ»Å 15 s Φ⌐ªΣ╕Çµ¼í∩╝êΣ╕ïΣ╕Çσá┤σÉîµê┐Θûôµ£âΦç¬σïòµÄÑΣ╕è∩╝ëπÇéµ░╕Σ╕ìσü£µ¡óπÇé
- µö╢σê░ `ping` σ¢₧ `pong`∩╝îΘü┐σàìΣ╝║µ£ìσÖ¿ΘûÆτ╜«ΘÇ╛µÖéµû╖τ╖ÜπÇé
- σÅ¬τö¿ `textContent`∩╝Å`createElement`∩╝¢ΘÇúτ╖ÜτïÇµàïσ»½σ£¿ `body[data-state]`∩╝êτò½Θ¥óΣ╕èΣ╕ìΘí»τñ║Σ╗╗Σ╜òτïÇµàïµûçσ¡ù∩╝ëπÇé
- σ¢₧µçëµ¿ÖΘá¡∩╝Ü`Cache-Control: no-cache`∩╝êσÉî repo µàúΣ╛ï∩╝ëπÇü`X-Content-Type-Options: nosniff`πÇüCSP `default-src 'none'; script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'`ΓÇª

## µ╕¼Φ⌐ª

```bash
cd sidecar/live-room
node --test tests/overlay.test.mjs                 # Node ΓëÑ18 σì│σÅ»∩╝ênode:test∩╝ë∩╝îΣ╕ìτö¿τ╡òσ░ìΦ╖»σ╛æ
python -m pytest tests/test_overlay_routes.py -q    # Σ╣ƒµ£âσæ╝σÅ½Σ╕èΘ¥óτÜä node µ╕¼Φ⌐ª∩╝¢τäí node σëç skip
python -m pytest tests -q                           # σà¿σÑù
```


## σ╖▓τƒÑΘÖÉσê╢πÇüσüçΦ¿¡Φêçσ╛àµ▒║σòÅΘíî

1. **ja Φ¡»µûçµ¼äΣ╜ì**∩╝Üτ¢«σëìΦºÇτ£╛τ½»τÖ╜σÉìσû«∩╝ê`app/dispatch.py` `_LISTENER_PASS`∩╝ëσÅ¬µ£ë `zh`πÇü`en`∩╝îΣ╕¡ΓåÆµùÑσá┤µ¼íτÜäΦ¡»µûçµÄ¿µ╕¼Σ╣ƒµö╛σ£¿ `en` µ¼äπÇéoverlay σ£¿ `lang=ja` µÖéΦïÑΣ║ïΣ╗╢σ╕╢ `ja` µ¼äµ£âσä¬σàêτö¿∩╝îσÉªσëçτö¿ `en`πÇéΦïÑΣ╣ïσ╛îµö╣µêÉ `tgt`/`tgt_lang` µ¼äΣ╜ì∩╝îΘ£ÇΦªüσ░Åµö╣ `lineText()`πÇé
2. **Σ╜öτö¿ΦºÇτ£╛σ╕¡Σ╜ì**∩╝Üoverlay µÿ»Σ╕ÇσÇïµÖ«ΘÇÜΦºÇτ£╛ΘÇúτ╖Ü∩╝îµ£âΦ¿êσàÑ `max_listeners`πÇéΦïÑΦªü OBS Σ╕ìΣ╜öΣ╜ì∩╝îΘ£ÇΦªüσ╛îτ½»σÅªΘûï host σ░êτö¿Θá╗Θüô∩╝êµ£¬σüÜ∩╝îσ¢áΣ╕ìΦâ╜µö╣ server.py∩╝ëπÇé
3. **Origin**∩╝ÜOBS σ╛₧σÉîΣ╕Ç host Φ╝ëσàÑΘáüΘ¥ó∩╝îOrigin Φêç Host τ¢╕σÉî∩╝îτ¼ªσÉê `audience_origin_allowed`πÇéΦïÑ OBS τö¿ `127.0.0.1` ΦÇî share host µÿ» LAN IP∩╝îΣ╣ƒµÿ»σÉîµ║ÉΦ½ïµ▒é∩╝îΣ╕ìσÅùσ╜▒Θƒ┐πÇé
4. µ£¬σ£¿τ£ƒτÜä OBS∩╝ÅCEF Σ╕èµëïσïòΘ⌐ùΦ¡ë∩╝êbox τäí OBS∩╝ë∩╝¢σ╗║Φ¡░µò┤σÉêΦÇàσ£¿ 5600H Σ╕èΘûïΣ╕Çµ¼íτó║Φ¬ìµÅÅΘéèΦêçµùÑµûçσ¡ùσ₧ïπÇé
5. `/overlay` ΘáüΘ¥óµ▓Æµ£ëΦ╡░ `RevalidatingStaticFiles` τÜä `?v=` µê│Φ¿ÿ∩╝êτé║Θü┐σàì overlay_routes σî»σàÑ server.py ΘÇáµêÉσ╛¬τÆ░σî»σàÑ∩╝ë∩╝¢Θ¥á `no-cache` Θçìµû░Θ⌐ùΦ¡ë∩╝îσ╖▓Φ╢│σñáπÇé

## BLOCKED
- τ¡åΘ¢╗µ▓Æµ£ë Node.js∩╝î`tests/overlay.test.mjs` τäíµ│òσ£¿τ¡åΘ¢╗σƒ╖Φíî∩╝êpytest σîàΦú¥µ£âΦç¬σïò skip∩╝ë∩╝¢σ╖▓σ£¿ box τö¿ Node 20∩╝Å22 Φ╖æΘüÄ 17/17πÇéΦïÑµò┤σÉêΦÇàΦªüσ£¿τ¡åΘ¢╗Φ╖æ∩╝îΘ£ÇΦªüσÅ»µö£σ╝Å Node ΓëÑ18∩╝êµ£¬Φç¬ΦíîΣ╕ïΦ╝ëσ«ëΦú¥∩╝ëπÇé
- τäí OBS σÅ»σüÜσ»ªµ⌐ƒτ¢«ΦªûΘ⌐ùΦ¡ë∩╝êµÅÅΘéèπÇüµùÑµûçσ¡ùσ₧ï fallback σ»ªΘÜ¢ΦÉ╜σ£¿σô¬σÑùσ¡ùσ₧ï∩╝ëπÇé

## σ«îµò┤ commit SHA
- τ¿ïσ╝Å commit∩╝Ü`727f35d8444b6aae5a4bb0a242c0e38af5109283`
- σƒ║µ║û∩╝Ü`bea2e5c3cac2ab7d3f35bdaa25cf2702c1b8894d`∩╝êgrok/integrate∩╝ë
