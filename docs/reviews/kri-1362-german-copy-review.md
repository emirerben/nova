# KRI-1362 German chat copy: review sheet

`src/apps/api/app/kria/copy/de.json` holds every chat sentence Kria can send in German. The key of each line is the English sentence from the code; the value is the German. When a chat is in German, `say()` looks the English up in this file and fills the `{...}` values in; a line missing from the file shows in English.

**All German here was drafted by AI (2026-11-10) and every line needs a German speaker's review before German replies are switched on.**

## How to change a line

1. Find the English sentence in `de.json` (it is the key; this sheet lists them all below).
2. Change only the German text after it. Keep every `{name}` exactly as written (including `!r` or `:.1f` parts), keep `\n` and any leading or trailing space, and keep the quote marks around button names.
3. Run `pytest tests/kria/test_copy_completeness.py` in `src/apps/api`; it fails if a `{name}` was dropped or renamed.

## Choices made in the draft

- Informal "du" everywhere.
- Terms: edit = **der Edit** (Talking-Edit, Voiceover-Edit, Narrated-Edit); caption = **Untertitel**; title = **Titel**; clip = **Clip**; take = **Take**; recording = **Aufnahme**; footage = **Material**; label = **Label**; pop-in = **Einblendung**; entrance animation = **Eingangsanimation**; freeze = **Standbild**; draft = **Entwurf**; approve = **freigeben**; render = **das Video erstellen**; speech cleanup = **Sprachbereinigung**; your talk = **deine Rede**; post caption = **Beitragstext**; lip-sync = **Lip-Sync**; cut (noun) = **Schnitt**.
- Receipt words: Done = Erledigt, Couldn't = Ging nicht, Partly done = Teilweise erledigt, Left out as agreed = Wie besprochen weggelassen.
- Seconds stay `s` with a space before it ("5 s"); numbers Kria fills in still use a dot ("2.5 s"), which is a code change if wanted.
- iPhone app buttons are still English in the app, so the German quotes them in English: „Refresh project“, „Clean up speech“, „Record with this script“.

### Chat button labels (one German wording each; sentences that quote them use the same words)

| English button | German |
|---|---|
| Make it without these | Ohne diese erstellen |
| Change my request | Meine Anfrage ändern |
| Keep it | So lassen |
| Change it | Ändern |
| Change it to {quoted} | In {quoted} ändern |
| Leave it out | Weglassen |
| Leave those clips out | Diese Clips weglassen |
| Continue without a title | Ohne Titel weiter |
| Continue without a fixed order | Ohne feste Reihenfolge weiter |
| Choose different clips | Andere Clips wählen |
| Yes, build it | Ja, erstellen |
| No, something's wrong | Nein, etwas stimmt nicht |
| No freeze | Kein Standbild |
| Every time | Jedes Mal |
| Use the order I give you | Reihenfolge nehmen, die ich dir gebe |
| Use the order you added the clips | Reihenfolge nehmen, in der du die Clips hinzugefügt hast |
| Extend it to {shown} seconds | Auf {shown} Sekunden verlängern |
| Make it {shown} seconds | {shown} Sekunden lang machen |
| Trim it to {asked_s} seconds | Auf {asked_s} Sekunden kürzen |
| Keep those {kept_s} seconds | Diese {kept_s} Sekunden behalten |
| Use the available {capacity_s} seconds | Die verfügbaren {capacity_s} Sekunden nehmen |
| End the edit when your voice ends ({voice_s} seconds) | Edit beenden, wenn deine Stimme endet ({voice_s} Sekunden) |
| Preserve the current word timings | Aktuelle Wort-Timings beibehalten |
| Lip-sync | Lip-Sync |
| Your song | Dein Song |
| Your voiceover | Dein Voiceover |
| No music | Keine Musik |

## Grammar fixed in code (KRI-1408)

The 125 lines this sheet listed as "needs a code change" (English plural endings, verbs, pronouns
or English words glued into a `{...}` value) were rewritten in KRI-1408: each sentence is now a
whole sentence per case (one clip / several clips, top / middle / bottom, first / last, ...), and
values that carried English text (language names, positions, format and pace names, song names,
lists joined with "and"/"or") now go through the catalog too. German "ich" mid-sentence (after
"; ") is lowercase, and long replies are shortened on a whole word.

Lines to review first: the 387 new lines and 45 reworded lines below.

### New lines (387)

| English | German | Turkish (for context) | OK? |
|---|---|---|---|
| "{other}" for {name} | "{other}" statt {name} | {name} yerine "{other}" |  |
| "{trigger}" at {at_s:.1f} s | „{trigger}“ bei {at_s:.1f} s | "{trigger}" {at_s:.1f} sn'de |  |
| Added the filming hour to {count} clip. | Aufnahmezeit bei {count} Clip ergänzt. | {count} klibe çekim saatini ekledim. |  |
| Added the filming hour to {count} clips. | Aufnahmezeit bei {count} Clips ergänzt. | {count} klibe çekim saatini ekledim. |  |
| Added {what} | {what} hinzugefügt | {what} eklendi |  |
| Aligned {who} to the center | {who} zentriert | {who} ortaya hizalandı |  |
| Aligned {who} to the left | {who} linksbündig ausgerichtet | {who} sola hizalandı |  |
| Aligned {who} to the right | {who} rechtsbündig ausgerichtet | {who} sağa hizalandı |  |
| Arabic | Arabisch | (language name, no Turkish key) |  |
| Balanced pacing | Tempo: Ausgewogen | Tempo: Dengeli |  |
| Changed the Bend of {who} | {who}: Biegung geändert | {who} için Kavis değişti |  |
| Changed the animation speed of {who} | {who}: Animationstempo geändert | {who} için animasyon hızı değişti |  |
| Changed the background behind {who}: blurred background | {who}: unscharfer Hintergrund | {who} arka planı değişti: bulanık arka plan |  |
| Changed the background behind {who}: photo or video behind it | {who}: Foto oder Video als Hintergrund | {who} arka planı değişti: arkasında fotoğraf ya da video |  |
| Changed the background behind {who}: plain | {who}: schlichter Hintergrund | {who} arka planı değişti: düz |  |
| Changed the background behind {who}: {kind} background | {who}: Hintergrund {kind} | {who} arka planı değişti: {kind} arka plan |  |
| Changed the background of {who} | {who}: Hintergrund geändert | {who} için arka plan değişti |  |
| Changed the behind-subject look of {who} | {who}: Hinter-dem-Motiv-Effekt geändert | {who} için özne arkası görünümü değişti |  |
| Changed the exit animation of {who} | {who}: Ausgangsanimation geändert | {who} için çıkış animasyonu değişti |  |
| Changed the highlight colour of {who} | {who}: Hervorhebungsfarbe geändert | {who} için vurgu rengi değişti |  |
| Changed the letter spacing of {who} | {who}: Zeichenabstand geändert | {who} için harf aralığı değişti |  |
| Changed the line spacing of {who} | {who}: Zeilenabstand geändert | {who} için satır aralığı değişti |  |
| Changed the looping animation of {who} | {who}: Schleifenanimation geändert | {who} için döngü animasyonu değişti |  |
| Changed the outline of {who} | {who}: Kontur geändert | {who} için kontur değişti |  |
| Changed the rotation of {who} | {who}: Drehung geändert | {who} için dönüş değişti |  |
| Changed the shadow of {who} | {who}: Schatten geändert | {who} için gölge değişti |  |
| Changed the text width of {who} | {who}: Textbreite geändert | {who} için yazı genişliği değişti |  |
| Changed {what} | {what} geändert | {what} değişti |  |
| Clip {names} doesn't look like "{words}" from what I saw, so I didn't put that wording on it. Tell me what it is, or ask me to describe what I see. | Clip {names} sieht nach dem, was ich gesehen habe, nicht nach "{words}" aus, deshalb habe ich diesen Text nicht daraufgesetzt. Sag mir, was darin zu sehen ist, oder bitte mich, zu beschreiben, was ich sehe. | Klip {names} gördüğüm kadarıyla "{words}" gibi görünmüyor, o yüzden bu ifadeyi koymadım. Ne olduğunu söyle ya da gördüğümü anlatmamı iste. |  |
| Clip {n} | Clip {n} | {n}. klip |  |
| Clip {which} can't be in this edit. | Clip {which} kann nicht in diesem Edit sein. | {which}. klip bu düzenlemeye giremiyor. |  |
| Clips {head} and {last} | Clips {head} und {last} | {v1} klipler |  |
| Clips {names} don't look like "{words}" from what I saw, so I didn't put that wording on them. Tell me what they are, or ask me to describe what I see. | Die Clips {names} sehen nach dem, was ich gesehen habe, nicht nach "{words}" aus, deshalb habe ich diesen Text nicht daraufgesetzt. Sag mir, was darin zu sehen ist, oder bitte mich, zu beschreiben, was ich sehe. | Klip {names} gördüğüm kadarıyla "{words}" gibi görünmüyor, o yüzden bu ifadeyi koymadım. Ne olduğunu söyle ya da gördüğümü anlatmamı iste. |  |
| Clips {which} can't be in this edit. | Die Clips {which} können nicht in diesem Edit sein. | {which}. klipler bu düzenlemeye giremiyor. |  |
| Danish | Dänisch | (language name, no Turkish key) |  |
| Day Vlog format | Format: Tagesvlog | Format: Günlük vlog |  |
| Dutch | Niederländisch | (language name, no Turkish key) |  |
| Each group plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them. | Jede Gruppe läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. | Her {tr_word} tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur. |  |
| Each sport plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them. | Jede Sportart läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. | Her {tr_word} tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur. |  |
| English | Englisch | (language name, no Turkish key) |  |
| Every video clip | Jeder Videoclip | Her video klibi |  |
| Fast pacing | Tempo: Schnell | Tempo: Hızlı |  |
| Filming hour on {len_timed} of {total} clips; {missing} has no filming time. | Die Aufnahmeuhrzeit steht bei {len_timed} von {total} Clips; {missing} hat keine Aufnahmezeit. | Çekim saati {total} klibin {len_timed} tanesinde var; {missing} klibin çekim saati yok. |  |
| Filming hour on {len_timed} of {total} clips; {missing} have no filming time. | Die Aufnahmeuhrzeit steht bei {len_timed} von {total} Clips; {missing} haben keine Aufnahmezeit. | Çekim saati {total} klibin {len_timed} tanesinde var; {missing} klibin çekim saati yok. |  |
| Freezes the frame for {seconds}s {where}, not the {requested:g}s you asked for: a freeze holds at least {seconds}s | Hält das Bild {seconds} s an, {where}, nicht die gewünschten {requested:g} s: ein Standbild dauert mindestens {seconds} s | Kare {seconds} sn donuyor, {where_tr}, istediğin {requested:g} sn değil: bir donma en {v2} {seconds} sn sürebiliyor |  |
| Freezes the frame for {seconds}s {where}, not the {requested:g}s you asked for: a freeze holds at most {seconds}s | Hält das Bild {seconds} s an, {where}, nicht die gewünschten {requested:g} s: ein Standbild dauert höchstens {seconds} s | Kare {seconds} sn donuyor, {where_tr}, istediğin {requested:g} sn değil: bir donma en {v2} {seconds} sn sürebiliyor |  |
| Freezes the frame for {seconds}s {where}; you didn't say how long, so I made it longer than {previous:g}s | Hält das Bild {seconds} s an, {where}; du hast nicht gesagt, wie lange, deshalb habe ich es länger als {previous:g} s gemacht | Kare {seconds} sn donuyor, {where_tr}; süre söylemediğin için {previous:g} sn'den {v2} tuttum |  |
| Freezes the frame for {seconds}s {where}; you didn't say how long, so I made it shorter than {previous:g}s | Hält das Bild {seconds} s an, {where}; du hast nicht gesagt, wie lange, deshalb habe ich es kürzer als {previous:g} s gemacht | Kare {seconds} sn donuyor, {where_tr}; süre söylemediğin için {previous:g} sn'den {v2} tuttum |  |
| French | Französisch | (language name, no Turkish key) |  |
| German | Deutsch | (language name, no Turkish key) |  |
| Greek | Griechisch | (language name, no Turkish key) |  |
| Group by group, chronological inside each group | In Gruppen ordnen, innerhalb jeder Gruppe chronologisch | Her {tr_word} bir blok olsun; blok içinde kronolojik sırala |  |
| Group by sport, chronological inside each sport | Nach Sportart gruppieren, innerhalb jeder Sportart chronologisch | Her {tr_word} bir blok olsun; blok içinde kronolojik sırala |  |
| How short should the clips be? | Wie kurz sollen die Clips sein? | Klipler ne kadar kısa olsun? |  |
| How short should the images be? | Wie kurz sollen die Bilder sein? | Görseller ne kadar kısa olsun? |  |
| How short should the videos be? | Wie kurz sollen die Videos sein? | Videolar ne kadar kısa olsun? |  |
| I can't cut a line out of a video made of several takes yet | aus einem Video aus mehreren Takes kann ich noch keinen Satz herausschneiden | birkaç çekimden oluşan bir videodan henüz satır kesemiyorum |  |
| I can't tell yet which clip is the talking one | ich weiß noch nicht, in welchem Clip du sprichst | hangi klibin konuşma klibi olduğunu henüz bilmiyorum |  |
| I could fit {planned} moment, not {count}. | Ich konnte {planned} Moment unterbringen, nicht {count}. | {count} değil, {planned} an sığdırabildim. |  |
| I could fit {planned} moments, not {count}. | Ich konnte {planned} Momente unterbringen, nicht {count}. | {count} değil, {planned} an sığdırabildim. |  |
| I couldn't confirm the part of the clip that plays shows the moment you named: {moments} | Ich konnte nicht bestätigen, dass der abgespielte Teil des Clips den Moment zeigt, den du genannt hast: {moments} | {v2}: {moments} |  |
| I couldn't find it in your take | ich habe es in deinem Take nicht gefunden | çekiminde bulamadım |  |
| I couldn't find the one you picked in your take | ich habe die Stelle, die du gewählt hast, in deinem Take nicht gefunden | seçtiğin anı çekiminde bulamadım |  |
| I couldn't leave out clip {numbers}, so it is still in the video. | Ich konnte Clip {numbers} nicht weglassen, deshalb ist er noch im Video. | {clips_tr} çıkaramadım, o yüzden videoda duruyor. |  |
| I couldn't leave out clips {numbers}, so they are still in the video. | Ich konnte die Clips {numbers} nicht weglassen, deshalb sind sie noch im Video. | {clips_tr} çıkaramadım, o yüzden videoda duruyorlar. |  |
| I couldn't suggest a {join_names} track this time. | Diesmal konnte ich dir keinen Song im Stil {join_names} vorschlagen. | Bu sefer bir {join_names_2} şarkısı öneremedim. |  |
| I couldn't suggest an {join_names} track this time. | Diesmal konnte ich dir keinen Song im Stil {join_names} vorschlagen. | Bu sefer bir {join_names_2} şarkısı öneremedim. |  |
| I cut this as a fast montage instead of a guided story: {v1}. | Ich habe das als schnelle Montage statt als geführte Story geschnitten: {v1}. | Bunu {label_tr} yerine hızlı montaj olarak kestim: {v2}. |  |
| I cut this as a fast montage instead of a text explainer: {v1}. | Ich habe das als schnelle Montage statt als Text-Erklärvideo geschnitten: {v1}. | Bunu {label_tr} yerine hızlı montaj olarak kestim: {v2}. |  |
| I didn't caption clip {join_skipped}. | Clip {join_skipped} habe ich nicht untertitelt. | Klip {join_skipped} için yazı eklemedim. |  |
| I didn't caption clips {join_skipped}. | Die Clips {join_skipped} habe ich nicht untertitelt. | Klip {join_skipped} için yazı eklemedim. |  |
| I found that moment ({start}–{end}s into the clip) but not the exact frame to hold, so it plays normally. Tell me the second to freeze on, like "freeze at {middle} seconds" | Ich habe diesen Moment gefunden ({start}–{end} s im Clip), aber nicht das genaue Bild zum Anhalten, deshalb läuft er normal. Sag mir, bei welcher Sekunde das Bild stehen soll, zum Beispiel „bei {middle} Sekunden einfrieren“ | {v1} (klibin {start}–{end}. saniyeleri) ama tutulacak tam kareyi bilmiyorum, o yüzden normal oynuyor. Hangi saniyede donsun, söyle; örneğin "{middle}. saniyede dondur" |  |
| I left out clip {join_unseen}: I can't tell what it shows. Tell me and I'll add it. | Clip {join_unseen} habe ich weggelassen: Ich erkenne nicht, was er zeigt. Sag es mir, dann ergänze ich ihn. | Klip {join_unseen} dışarıda kaldı: ne gösterdiğini anlayamadım. Söylersen ekleyeyim. |  |
| I left out clip {numbers} because it is too short to use. | Ich habe Clip {numbers} weggelassen, weil er zu kurz ist, um ihn zu verwenden. | {clips_tr} kullanılamayacak kadar kısa olduğu için çıkardım. |  |
| I left out clip {which}. | Ich habe Clip {which} weggelassen. | {which}. klibi dışarıda bıraktım. |  |
| I left out clips {join_unseen}: I can't tell what they show. Tell me and I'll add them. | Die Clips {join_unseen} habe ich weggelassen: Ich erkenne nicht, was sie zeigen. Sag es mir, dann ergänze ich sie. | Klip {join_unseen} dışarıda kaldı: ne gösterdiğini anlayamadım. Söylersen ekleyeyim. |  |
| I left out clips {numbers} because they are too short to use. | Ich habe die Clips {numbers} weggelassen, weil sie zu kurz sind, um sie zu verwenden. | {clips_tr} kullanılamayacak kadar kısa oldukları için çıkardım. |  |
| I left out clips {which}. | Ich habe die Clips {which} weggelassen. | {which}. klipleri dışarıda bıraktım. |  |
| I matched {by_lyrics} take by your singing. | Ich habe {by_lyrics} Take anhand deines Gesangs zugeordnet. | {by_lyrics} çekimi söylediğin sözlere göre eşleştirdim. |  |
| I matched {by_lyrics} takes by your singing. | Ich habe {by_lyrics} Takes anhand deines Gesangs zugeordnet. | {by_lyrics} çekimi söylediğin sözlere göre eşleştirdim. |  |
| I prepared {count} reversible editor change. | Ich habe {count} Änderung im Editor vorbereitet, die du rückgängig machen kannst. | Editörde {count} geri alınabilir değişiklik hazırladım. |  |
| I prepared {count} reversible editor changes. | Ich habe {count} Änderungen im Editor vorbereitet, die du rückgängig machen kannst. | Editörde {count} geri alınabilir değişiklik hazırladım. |  |
| I ticked the first clip you filmed for "{category_get}". | Ich habe für "{category_get}" den ersten Clip angehakt, den du gefilmt hast. | "{category_get}" için çektiğin ilk klibi işaretledim. |  |
| I ticked the last clip you filmed for "{category_get}". | Ich habe für "{category_get}" den letzten Clip angehakt, den du gefilmt hast. | "{category_get}" için çektiğin son klibi işaretledim. |  |
| I used all {len_used} clips, including the {len_short} short one | Ich habe alle {len_used} Clips verwendet, auch {len_short} kurzen Clip | {len_used} klibin hepsini kullandım, {len_short} kısa klip de dahil |  |
| I used all {len_used} clips, including the {len_short} short ones | Ich habe alle {len_used} Clips verwendet, auch die {len_short} kurzen | {len_used} klibin hepsini kullandım, {len_short} kısa klip de dahil |  |
| I used {len_used} clips, including the {len_short} short one | Ich habe {len_used} Clips verwendet, auch {len_short} kurzen Clip | {len_used} klibin tamamını kullandım, {len_short} kısa klip de dahil |  |
| I used {len_used} clips, including the {len_short} short ones | Ich habe {len_used} Clips verwendet, auch die {len_short} kurzen | {len_used} klibin tamamını kullandım, {len_short} kısa klip de dahil |  |
| I'll cut this as a fast montage instead of a guided story: {v1} | Ich schneide das als schnelle Montage statt als geführte Story: {v1} | Bunu {label_tr} yerine hızlı montaj olarak keseceğim: {v2} |  |
| I'll cut this as a fast montage instead of a text explainer: {v1} | Ich schneide das als schnelle Montage statt als Text-Erklärvideo: {v1} | Bunu {label_tr} yerine hızlı montaj olarak keseceğim: {v2} |  |
| I'll keep your take from {at_start} on, if I find that line; if not, it opens at the start | Ich behalte deinen Take ab {at_start}, wenn ich diesen Satz finde; sonst beginnt das Video am Anfang | {v1}{at_start} cümlesinden sonrasını korurum, o cümleyi bulursam; bulamazsam video baştan açılır |  |
| I'll keep your take from {at_start} to right after {at_end}, if I find those lines | Ich behalte deinen Take von {at_start} bis kurz nach {at_end}, wenn ich diese Sätze finde | {v1}{at_start} cümlesinden {at_end} cümlesinin hemen sonrasına kadar olan kısmını korurum, bu cümleleri bulursam |  |
| I'll keep your take up to right after {at_end}, if I find that line; if not, it runs to the end | Ich behalte deinen Take bis kurz nach {at_end}, wenn ich diesen Satz finde; sonst läuft das Video bis zum Ende | {v1}{at_end} cümlesinin hemen sonrasına kadar olan kısmını korurum, o cümleyi bulursam; bulamazsam video sonuna kadar sürer |  |
| I'll show it on the {word} you picked, at {clock} in your take; if I can't find that one, it won't appear | Ich zeige ihn bei dem von dir gewählten {word}, bei {clock} in deinem Take; finde ich diese Stelle nicht, erscheint er nicht | {v1}{word} anında, çekiminin {clock} noktasında gösteririm; o anı bulamazsam yazı çıkmaz |  |
| I'll spell {and_names_names} the way you wrote it and check it in the finished video | ich schreibe {and_names_names} so, wie du es geschrieben hast, und prüfe es im fertigen Video | şunu senin yazdığın gibi yazıp bitmiş videoda kontrol edeceğim: {and_names_names} |  |
| I'll spell {and_names_names} the way you wrote them and check them in the finished video | ich schreibe {and_names_names} so, wie du sie geschrieben hast, und prüfe sie im fertigen Video | şunları senin yazdığın gibi yazıp bitmiş videoda kontrol edeceğim: {and_names_names} |  |
| Italian | Italienisch | (language name, no Turkish key) |  |
| Keep it strictly chronological; groups may interleave | Streng chronologisch lassen; die Gruppen dürfen sich mischen | Tam kronolojik kalsın; {tr_word} klipleri iç içe geçebilir |  |
| Keep it strictly chronological; sports may interleave | Streng chronologisch lassen; die Sportarten dürfen sich mischen | Tam kronolojik kalsın; {tr_word} klipleri iç içe geçebilir |  |
| Korean | Koreanisch | (language name, no Turkish key) |  |
| Labelled {count} clip. | {count} Clip beschriftet. | {count} klibe etiket ekledim. |  |
| Labelled {count} clips. | {count} Clips beschriftet. | {count} klibe etiket ekledim. |  |
| Lined up {who} on the center | {who} in die Mitte gerückt | {who} ortaya hizalandı |  |
| Lined up {who} on the left | {who} nach links gerückt | {who} sola hizalandı |  |
| Lined up {who} on the right | {who} nach rechts gerückt | {who} sağa hizalandı |  |
| Made {who} bigger ({old:g}→{new:g}px) | {who} vergrößert ({old:g}→{new:g}px) | {who} büyüttüm ({old:g}→{new:g}px) |  |
| Made {who} smaller ({old:g}→{new:g}px) | {who} verkleinert ({old:g}→{new:g}px) | {who} küçülttüm ({old:g}→{new:g}px) |  |
| Montage format | Format: Montage | Format: Montaj |  |
| Moved the captions to the bottom | Untertitel nach unten verschoben | Altyazılar alt konuma taşındı |  |
| Moved the captions to the middle | Untertitel in die Mitte verschoben | Altyazılar orta konuma taşındı |  |
| Moved the captions to the top | Untertitel nach oben verschoben | Altyazılar üst konuma taşındı |  |
| Moved {who} to the bottom | {who} nach unten verschoben | {who} alt konuma taşındı |  |
| Moved {who} to the middle | {who} in die Mitte verschoben | {who} orta konuma taşındı |  |
| Moved {who} to the top | {who} nach oben verschoben | {who} üst konuma taşındı |  |
| Narrated Planned format | Format: Mit Voiceover | Format: Seslendirmeli |  |
| Narrated Ready format | Format: Mit Voiceover | Format: Seslendirmeli |  |
| Narrated format | Format: Mit Voiceover | Format: Seslendirmeli |  |
| No filming time for clip {join_untimed}. | Keine Aufnahmezeit für Clip {join_untimed}. | Klip {join_untimed} için çekim saati yok. |  |
| No filming time for clips {join_untimed}. | Keine Aufnahmezeit für die Clips {join_untimed}. | Klip {join_untimed} için çekim saati yok. |  |
| None of the {total} clips got its own text in this draft. | Keiner der {total} Clips hat in diesem Entwurf einen eigenen Text bekommen. | Bu taslakta {total} klibin hiçbirine kendi yazısı gelmedi. |  |
| Only clip {which} is in this edit and it runs about {possible:g} seconds, so the video can't be {wanted:g} seconds. | In diesem Edit ist nur Clip {which}, und er dauert etwa {possible:g} Sekunden, deshalb kann das Video nicht {wanted:g} Sekunden lang sein. | Bu düzenlemede yalnızca {which}. klip var ve yaklaşık {possible:g} saniye sürüyor, bu yüzden video {wanted:g} saniye olamıyor. |  |
| Only clips {which} are in this edit and together they run about {possible:g} seconds, so the video can't be {wanted:g} seconds. | In diesem Edit sind nur die Clips {which}, und zusammen dauern sie etwa {possible:g} Sekunden, deshalb kann das Video nicht {wanted:g} Sekunden lang sein. | Bu düzenlemede yalnızca {which}. klipler var ve birlikte yaklaşık {possible:g} saniye sürüyor, bu yüzden video {wanted:g} saniye olamıyor. |  |
| Optional: move the {word} to the bottom so it's easier to read. | Optional: {word} nach unten verschieben, damit der Text besser lesbar ist. | İstersen {word} daha kolay okunsun diye alta taşınabilir. |  |
| Optional: move the {word} to the middle so it's easier to read. | Optional: {word} in die Mitte verschieben, damit der Text besser lesbar ist. | İstersen {word} daha kolay okunsun diye ortaya taşınabilir. |  |
| Optional: move the {word} to the top so it's easier to read. | Optional: {word} nach oben verschieben, damit der Text besser lesbar ist. | İstersen {word} daha kolay okunsun diye üste taşınabilir. |  |
| Pop-in on {joined} | Pop-in: {joined} | {join_parts_tr}{tail_tr} çıkıyor |  |
| Pop-ins on {joined} | Pop-ins: {joined} | {join_parts_tr}{tail_tr} çıkıyor |  |
| Portuguese | Portugiesisch | (language name, no Turkish key) |  |
| Relaxed pacing | Tempo: Entspannt | Tempo: Sakin |  |
| Removed {what} | {what} entfernt | {what} kaldırıldı |  |
| Serbian | Serbisch | (language name, no Turkish key) |  |
| Set {who} to Title Case | {who}: Jedes Wort beginnt jetzt groß | {who} → Baş Harfler Büyük |  |
| Set {who} to UPPERCASE | {who} in GROSSBUCHSTABEN gesetzt | {who} → BÜYÜK HARF |  |
| Set {who} to a new case | {who}: Schreibweise geändert | {who} → yeni harf biçimi |  |
| Set {who} to its original case | {who} wieder in der ursprünglichen Schreibweise | {who} → özgün harf |  |
| Set {who} to lowercase | {who} in kleinbuchstaben gesetzt | {who} → küçük harf |  |
| Single Hero format | Format: Hauptclip | Format: Tek kahraman klip |  |
| Slides format | Format: Folien | Format: Slayt |  |
| Spanish | Spanisch | (language name, no Turkish key) |  |
| Speech cleanup cut what it heard as {joined} ({seconds:.1f} s). | Die Sprachbereinigung hat geschnitten, was sie dafür hielt: {joined} ({seconds:.1f} s). | Konuşma temizliği duyduğu şu sesleri kesti: {joined_tr} ({seconds_tr} sn). |  |
| Speech cleanup cut what it heard as {joined}. | Die Sprachbereinigung hat geschnitten, was sie dafür hielt: {joined}. | Konuşma temizliği duyduğu şu sesleri kesti: {joined_tr}. |  |
| Still in your video: "{quote}" ({v1}) | Noch in deinem Video: „{quote}“ ({v1}) | {v2}: "{quote}" ({v1}) |  |
| Subtitled format | Format: Mit Untertiteln | Format: Altyazılı |  |
| Talking Head format | Format: Talking Head | Format: Kameraya konuşma |  |
| Text landed on {count} of {total} clips. | Text ist auf {count} von {total} Clips gelandet. | Yazı {total} klibin {count} tanesine geldi. |  |
| Text on {count} clips. | Text auf {count} Clips. | {count} klipte yazı var. |  |
| That footage cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem Material kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That footage cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem Material kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That part can't make a {asked_s}-second edit without changing that line. | Aus diesem Teil kann ich keinen {asked_s}-Sekunden-Edit machen, ohne diesen Satz zu ändern. | {lines_tr} değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That part can't make a {asked_s}-second edit without changing those lines. | Aus diesem Teil kann ich keinen {asked_s}-Sekunden-Edit machen, ohne diese Sätze zu ändern. | {lines_tr} değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That part runs longer than {asked_s} seconds, and I can't shorten it without changing that line. | Dieser Teil ist länger als {asked_s} Sekunden, und ich kann ihn nicht kürzen, ohne diesen Satz zu ändern. | O kısım {asked_s} saniyeden uzun ve {lines_tr} değiştirmeden kısaltamam. |  |
| That part runs longer than {asked_s} seconds, and I can't shorten it without changing those lines. | Dieser Teil ist länger als {asked_s} Sekunden, und ich kann ihn nicht kürzen, ohne diese Sätze zu ändern. | O kısım {asked_s} saniyeden uzun ve {lines_tr} değiştirmeden kısaltamam. |  |
| That song cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem Song kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That song cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem Song kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That spoken take cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem gesprochenen Take kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That spoken take cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem gesprochenen Take kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That voiceover with the selected footage cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem Voiceover und dem ausgewählten Material kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That voiceover with the selected footage cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem Voiceover und dem ausgewählten Material kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| The captions show up to {drawn_words} words at a time, not one. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht nur eines. | Altyazılar aynı anda bir değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions show up to {drawn_words} words at a time, not three. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht drei. | Altyazılar aynı anda üç değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions show up to {drawn_words} words at a time, not two. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht zwei. | Altyazılar aynı anda iki değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions sit higher than you asked so they stay off your face. | Die Untertitel sitzen höher als gewünscht, damit sie nicht über deinem Gesicht liegen. | Altyazılar yüzüne gelmesin diye istediğinden daha yukarıda duruyor. |  |
| The captions sit lower than you asked so they stay off your face. | Die Untertitel sitzen tiefer als gewünscht, damit sie nicht über deinem Gesicht liegen. | Altyazılar yüzüne gelmesin diye istediğinden daha aşağıda duruyor. |  |
| The captions won't be held to one words at a time. | Die Untertitel werden nicht auf ein Wort auf einmal begrenzt. | Altyazılar aynı anda bir kelimeyle sınırlı kalmayacak. |  |
| The captions won't be held to three words at a time. | Die Untertitel werden nicht auf drei Wörter auf einmal begrenzt. | Altyazılar aynı anda üç kelimeyle sınırlı kalmayacak. |  |
| The captions won't be held to two words at a time. | Die Untertitel werden nicht auf zwei Wörter auf einmal begrenzt. | Altyazılar aynı anda iki kelimeyle sınırlı kalmayacak. |  |
| The captions write {wrong_spellings}. You can fix that in the editor. | In den Untertiteln steht {wrong_spellings}. Das kannst du im Editor korrigieren. | Altyazılarda {wrong_spellings} yazıyor. Bunu editörde düzeltebilirsin. |  |
| The footage sound still plays at full volume alongside your voice; {fix} | Der Originalton läuft noch in voller Lautstärke neben deiner Stimme; {fix} | {v1}, sesinle aynı seviyede; {fix} |  |
| The footage sound still plays softly under your voice (about {pct}%); {fix} | Der Originalton läuft noch leise unter deiner Stimme (etwa {pct} %); {fix} | {v1}, sesinin altında kısık{about_tr}; {fix} |  |
| The footage sound still plays softly under your voice; {fix} | Der Originalton läuft noch leise unter deiner Stimme; {fix} | {v1}, sesinin altında kısık{about_tr}; {fix} |  |
| The selected footage runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter footage. | Das ausgewählte Material dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm kürzeres Material. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected footage runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Das ausgewählte Material dauert {natural_duration:g} Sekunden, reicht ohne Wiederholung aber nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected song runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter song. | Der ausgewählte Song dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm einen kürzeren Song. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected song runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Der ausgewählte Song dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected take runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter take. | Der ausgewählte Take dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm einen kürzeren Take. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected take runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Der ausgewählte Take dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected voiceover runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter voiceover. | Das ausgewählte Voiceover dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm ein kürzeres Voiceover. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected voiceover runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Das ausgewählte Voiceover dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The video has {len_lengths} moment, not {ask_count} | Das Video hat {len_lengths} Moment, nicht {ask_count} | Videoda {ask_count} değil {len_lengths} an var |  |
| The video has {len_lengths} moments, not {ask_count} | Das Video hat {len_lengths} Momente, nicht {ask_count} | Videoda {ask_count} değil {len_lengths} an var |  |
| This day vlog edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Tagesvlog-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (günlük vlog) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This draft is about {round_s_facts_duration_s}s{outro}; you asked for at most {asked}s. | Dieser Entwurf ist etwa {round_s_facts_duration_s} s lang{outro}; du wolltest höchstens {asked} s. | Bu taslak yaklaşık {round_s_facts_duration_s} saniye{outro}; sen {asked_tr} istedin. |  |
| This draft is about {round_s_facts_duration_s}s{outro}; you asked for {asked}s. | Dieser Entwurf ist etwa {round_s_facts_duration_s} s lang{outro}; du wolltest {asked} s. | Bu taslak yaklaşık {round_s_facts_duration_s} saniye{outro}; sen {asked_tr} istedin. |  |
| This draft is ordered by attachment order, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, in der du die Clips hinzugefügt hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: klipleri eklediğin sıra. |  |
| This draft is ordered by attachment, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, in der du die Clips hinzugefügt hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: klipleri eklediğin sıra. |  |
| This draft is ordered by confirmed, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, die du bestätigt hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: onayladığın sıra. |  |
| This draft is ordered by creator order, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, die du angegeben hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: senin verdiğin sıra. |  |
| This draft is ordered by editor, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge im Editor, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: editördeki sıra. |  |
| This draft is ordered by song time, not the order you asked for. | Dieser Entwurf ist nach der Zeit im Song geordnet, nicht in der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: şarkının zamanı. |  |
| This montage edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Montage-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (montaj) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This narrated edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Voiceover-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (seslendirmeli) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This narrated planned edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Voiceover-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (seslendirmeli) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This narrated ready edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Voiceover-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (seslendirmeli) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This single hero edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Edit rund um einen Hauptclip kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (tek kahraman) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This slides edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Slideshow-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (slayt) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This talking head edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Talking-Head-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (konuşmalı) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This version leaves out the photo you asked for: "{ask}". | In dieser Version fehlt das Foto, das du dir gewünscht hast: "{ask}". | Bu sürümde istediğin fotoğraf yok: "{ask}". |  |
| This version leaves out the video you asked for: "{ask}". | In dieser Version fehlt das Video, das du dir gewünscht hast: "{ask}". | Bu sürümde istediğin video yok: "{ask}". |  |
| Turkish | Türkisch | (language name, no Turkish key) |  |
| Use the supported trim for this footage. | Nutzt die unterstützte Kürzung für dieses Material. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use the supported trim for this song. | Nutzt die unterstützte Kürzung für diesen Song. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use the supported trim for this spoken take. | Nutzt die unterstützte Kürzung für diesen gesprochenen Take. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use the supported trim for this voiceover with the selected footage. | Nutzt die unterstützte Kürzung für dieses Voiceover mit dem ausgewählten Material. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| With the standard ending that part runs to {asked_s} seconds or more, and I can't shorten it without changing that line. | Mit dem Standard-Ende dauert dieser Teil {asked_s} Sekunden oder länger, und ich kann ihn nicht kürzen, ohne diesen Satz zu ändern. | Standart kapanışla o kısım {asked_s} saniyeye ya da daha fazlasına çıkıyor ve {lines_tr} değiştirmeden kısaltamam. |  |
| With the standard ending that part runs to {asked_s} seconds or more, and I can't shorten it without changing those lines. | Mit dem Standard-Ende dauert dieser Teil {asked_s} Sekunden oder länger, und ich kann ihn nicht kürzen, ohne diese Sätze zu ändern. | Standart kapanışla o kısım {asked_s} saniyeye ya da daha fazlasına çıkıyor ve {lines_tr} değiştirmeden kısaltamam. |  |
| You asked for a chronological video and for the clips grouped by group. | Du wolltest ein chronologisches Video und die Clips in Gruppen sortiert. | Hem kronolojik bir video hem de kliplerin {tr_word} bazında gruplanmasını istedin. |  |
| You asked for a chronological video and for the clips grouped by sport. | Du wolltest ein chronologisches Video und die Clips nach Sportart gruppiert. | Hem kronolojik bir video hem de kliplerin {tr_word} bazında gruplanmasını istedin. |  |
| You picked {clips} clip, so "{pin_text}" can't go on clip {pin_clip}. Which clip should it be on? | Du hast {clips} Clip ausgewählt, deshalb kann "{pin_text}" nicht auf Clip {pin_clip} kommen. Auf welchen Clip soll es stattdessen? | {clips} klip seçtin, o yüzden "{pin_text}" {pin_clip}. klibe konamaz. Hangi klibe koyayım? |  |
| You picked {clips} clips, so "{pin_text}" can't go on clip {pin_clip}. Which clip should it be on? | Du hast {clips} Clips ausgewählt, deshalb kann "{pin_text}" nicht auf Clip {pin_clip} kommen. Auf welchen Clip soll es stattdessen? | {clips} klip seçtin, o yüzden "{pin_text}" {pin_clip}. klibe konamaz. Hangi klibe koyayım? |  |
| You'll start full screen in your own room, then you're {corner_0} when you start on your first recording. | Du startest im Vollbild in deinem eigenen Raum, dann bist du {corner_0}, sobald deine erste Aufnahme beginnt. | {v2}, sonra ilk kaydına geçince {corner_1}sın. |  |
| You'll start full screen in your own room, then your background takes the screen. | Du startest im Vollbild in deinem eigenen Raum, dann übernimmt dein Hintergrund den Bildschirm. | {v2}, sonra arka plan ekranı alıyor. |  |
| You'll start full screen in your own room, then {corner_0}. | Du startest im Vollbild in deinem eigenen Raum, danach bist du {corner_0}. | {v2}, sonra {corner_1}sın. |  |
| You're full screen in your own take for the first {seconds} s, then your background takes the screen. | Du bist in den ersten {seconds} s deines eigenen Takes im Vollbild, dann übernimmt dein Hintergrund den Bildschirm. | {v2}, sonra arka plan ekranı alıyor. |  |
| You're full screen in your own take for the first {seconds} s, then {corner_0}. | Du bist in den ersten {seconds} s deines eigenen Takes im Vollbild, danach bist du {corner_0}. | {v2}, sonra {corner_1}sın. |  |
| Your brief says "{said}", but the available footage is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber das verfügbare Material reicht für {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your brief says "{said}", but the available song is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber der verfügbare Song dauert {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your brief says "{said}", but the available spoken take is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber der verfügbare gesprochene Take dauert {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your brief says "{said}", but the available voiceover with the selected footage is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber das verfügbare Voiceover mit dem ausgewählten Material dauert {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your clips and photos have no filming time, so I can't put them in the order you filmed. | Keiner deiner Clips und Fotos hat eine Aufnahmezeit, deshalb kann ich sie nicht in die Reihenfolge deiner Aufnahmen bringen. | Kliplerinin ve fotoğraflarının hiçbiri için çekim saati yok, o yüzden onları çektiğin sıraya koyamıyorum. |  |
| Your end line keeps about {kept_s} seconds of your take: from the start to right after {end}. | Dein Schlusssatz behält etwa {kept_s} Sekunden deines Takes: vom Anfang bis kurz nach {end}. | Bitiş cümlen çekiminin yaklaşık {kept_s} saniyesini tutuyor: baştan {end} cümlesinin hemen sonrasına kadar. |  |
| Your start and end lines keep about {kept_s} seconds of your take: from {start} to right after {end}. | Dein Startsatz und dein Schlusssatz behalten etwa {kept_s} Sekunden deines Takes: von {start} bis kurz nach {end}. | Başlangıç ve bitiş cümlelerin çekiminin yaklaşık {kept_s} saniyesini tutuyor: {start} cümlesinden {end} cümlesinin hemen sonrasına kadar. |  |
| Your start line keeps about {kept_s} seconds of your take: from {start} to the end. | Dein Startsatz behält etwa {kept_s} Sekunden deines Takes: von {start} bis zum Ende. | Başlangıç cümlen çekiminin yaklaşık {kept_s} saniyesini tutuyor: {start} cümlesinden sona kadar. |  |
| a fast montage keeps its short cuts | Eine schnelle Montage behält ihre kurzen Schnitte | hızlı montaj kısa kesimlerini koruyor |  |
| a slide post shows each slide for its own time | Ein Slide-Post zeigt jede Slide so lange, wie sie selbst dauert | slayt gönderisi her slaytı kendi süresince gösteriyor |  |
| a slide post shows your slides in order | Ein Slide-Post zeigt deine Slides der Reihe nach | slayt gönderisi slaytlarını sırayla gösteriyor |  |
| a slide post's length follows its slides | Die Länge eines Slide-Posts richtet sich nach seinen Slides | slayt gönderisinin uzunluğu slaytlarına bağlı |  |
| analyze clips | Clips analysieren | analyze clips |  |
| assemble | Zusammensetzen | assemble |  |
| at most {asked}s | höchstens {asked} s | en fazla {asked} saniye |  |
| at the second you picked ({at}s into the clip) | an der Sekunde, die du gewählt hast ({at} s im Clip) | seçtiğin saniyede (klibin {at}. saniyesi) |  |
| balanced | ausgewogen | balanced |  |
| cutting it out of your voiceover failed | das Herausschneiden aus deinem Voiceover ist fehlgeschlagen | seslendirmenden kesmek başarısız oldu |  |
| cutting it would leave too little video | wenn ich es schneide, bleibt zu wenig Video übrig | kesersem çok az video kalırdı |  |
| day vlog | Tagesvlog | day vlog |  |
| download clips | Clips laden | download clips |  |
| fast | schnell | fast |  |
| fast montage | schnelle Montage | fast montage |  |
| finalize | Fertigstellen | finalize |  |
| first clip you added | ersten Clip, den du hinzugefügt hast | eklediğin ilk klip |  |
| first clip you filmed | ersten Clip, den du gefilmt hast | çektiğin ilk klip |  |
| generate copy | Texte schreiben | generate copy |  |
| guided story | geführte Story | guided story |  |
| it is cut around the spoken lines | Es wird rund um die gesprochenen Sätze geschnitten | konuşulan cümlelerin etrafında kesiliyor |  |
| it is cut as a fast montage | Es wird als schnelle Montage geschnitten | hızlı bir montaj olarak kesiliyor |  |
| it is cut as a montage under your voiceover | Es wird als Montage unter deinem Voiceover geschnitten | seslendirmenin altında bir montaj olarak kesiliyor |  |
| it is cut to your song | Es wird passend zu deinem Song geschnitten | şarkına göre kesiliyor |  |
| its length follows your song | Die Länge richtet sich nach deinem Song | uzunluğu şarkını takip ediyor |  |
| its length follows your speech | Die Länge richtet sich nach dem, was du sagst | uzunluğu konuşmanı takip ediyor |  |
| its length follows your voiceover | Die Länge richtet sich nach deinem Voiceover | uzunluğu seslendirmeni takip ediyor |  |
| last clip you added | letzten Clip, den du hinzugefügt hast | eklediğin son klip |  |
| last clip you filmed | letzten Clip, den du gefilmt hast | çektiğin son klip |  |
| match clips | Clips zuordnen | match clips |  |
| match song | Song auswählen | match song |  |
| mix audio | Ton mischen | mix audio |  |
| montage | Montage | montage |  |
| narrated | mit Voiceover | narrated |  |
| narrated planned | mit Voiceover | narrated planned |  |
| narrated ready | mit Voiceover | narrated ready |  |
| native | nativ | native |  |
| on that moment ({at}s into the clip) | bei diesem Moment ({at} s im Clip) | o anda (klibin {at}. saniyesi) |  |
| on that moment ({at}s into the clip; you left the exact frame to me) | bei diesem Moment ({at} s im Clip; das genaue Bild hast du mir überlassen) | o anda (klibin {at}. saniyesi; tam kareyi bana bıraktın) |  |
| on the last frame of the last shot | auf dem letzten Bild der letzten Einstellung | son çekimin son karesinde |  |
| on the moment you picked, at its middle ({at}s into the clip) | in der Mitte des Moments, den du gewählt hast ({at} s im Clip) | seçtiğin anın ortasında (klibin {at}. saniyesi) |  |
| photo {number} | Foto {number} | {number}. fotoğraf |  |
| queued | Warteschlange | queued |  |
| relaxed | entspannt | relaxed |  |
| render variants | Varianten erstellen | render variants |  |
| single hero | Hauptclip | single hero |  |
| slides | Folien | slides |  |
| small in a corner | klein in einer Ecke | köşede küçük |  |
| small in the bottom-left corner | klein in der unteren linken Ecke | sol alt köşede küçük |  |
| small in the bottom-right corner | klein in der unteren rechten Ecke | sağ alt köşede küçük |  |
| small in the top-left corner | klein in der oberen linken Ecke | sol üst köşede küçük |  |
| small in the top-right corner | klein in der oberen rechten Ecke | sağ üst köşede küçük |  |
| subtitled | mit Untertiteln | subtitled |  |
| talking head | Talking Head | talking head |  |
| tell me to mute it and I'll take it out | sag mir, dass ich ihn stummschalten soll, dann nehme ich ihn raus | kapatmamı söylersen çıkarırım |  |
| text explainer | Text-Erklärvideo | text explainer |  |
| that part is about {kept}s | dieser Teil ist etwa {kept} s lang | o kısım yaklaşık {kept} saniye |  |
| the clips can't each be held long enough for it within the length you asked for | In der gewünschten Länge kann ich nicht jeden Clip lange genug dafür zeigen | klipler, istediğin süre içinde bunun için yeterince uzun tutulamıyor |  |
| the clips' own spoken lines are kept whole, which only the fast-montage layout guarantees | Die gesprochenen Sätze in den Clips bleiben ganz, und das garantiert nur das Layout der schnellen Montage | kliplerin kendi konuşmaları bütün tutuluyor, bunu sadece hızlı montaj düzeni garanti ediyor |  |
| the cuts between spoken lines keep a fixed length | Die Schnitte zwischen den gesprochenen Sätzen haben eine feste Länge | konuşmalar arasındaki kesimler sabit uzunlukta |  |
| the cuts follow a fast pace | Die Schnitte folgen einem schnellen Tempo | kesimler hızlı tempoda |  |
| the cuts follow your voiceover | Die Schnitte folgen deinem Voiceover | kesimler seslendirmeni izliyor |  |
| the cuts keep their usual length | Die Schnitte behalten ihre übliche Länge | kesimler her zamanki uzunluğunda |  |
| the first clip you added | dem ersten Clip, den du hinzugefügt hast | eklediğin ilk klip |  |
| the first clip you filmed | dem ersten Clip, den du gefilmt hast | çektiğin ilk klip |  |
| the footage doesn't fit that layout | Das Material passt nicht zu diesem Layout | görüntüler bu düzene uymuyor |  |
| the last clip you added | dem letzten Clip, den du hinzugefügt hast | eklediğin son klip |  |
| the last clip you filmed | dem letzten Clip, den du gefilmt hast | çektiğin son klip |  |
| the plan has no story outline to build chapters from | Der Plan hat keine Story-Gliederung, aus der ich Kapitel bauen könnte | bölümleri oluşturacak bir hikâye taslağı yok |  |
| the same clip plays more than once, which only the fast-montage layout allows | Derselbe Clip läuft mehr als einmal, und das erlaubt nur das Layout der schnellen Montage | aynı klip birden fazla kez oynuyor, buna sadece hızlı montaj düzeni izin veriyor |  |
| the story text couldn't be placed on it | Der Story-Text ließ sich nicht darauf platzieren | hikâye yazısı buna yerleştirilemedi |  |
| the talking clip is {take}s | der Clip, in dem du sprichst, ist {take} s lang | konuşma klibi {take} saniye |  |
| the video follows your speech | Das Video folgt dem, was du sagst | video konuşmanı izliyor |  |
| the video follows your voiceover | Das Video folgt deinem Voiceover | video seslendirmeni izliyor |  |
| there are too many clips to lay out as story chapters | Es sind zu viele Clips, um sie als Story-Kapitel anzuordnen | bölümlere ayırmak için çok fazla klip var |  |
| this edit has no control to turn it down yet | in diesem Edit gibt es noch keine Einstellung, um ihn leiser zu machen | bu düzenlemede onu kısacak bir ayar henüz yok |  |
| this editor can only restage it as a fast montage, so nothing changed. A different direction needs a fresh edit | Dieser Editor kann es nur als schnelle Montage neu aufbauen, deshalb hat sich nichts geändert. Für eine andere Richtung brauchst du einen neuen Edit | bu editör onu yalnızca hızlı bir montaj olarak yeniden kurabiliyor, o yüzden hiçbir şey değişmedi. Başka bir yön için yeni bir düzenleme gerekiyor |  |
| turn it down in the editor under Sounds > Original audio, or tell me to mute it | stell ihn im Editor unter „Sounds > Original audio“ leiser oder sag mir, dass ich ihn stummschalten soll | editörde Sesler > Orijinal ses bölümünden kıs ya da bana kapatmamı söyle |  |
| upload | Hochladen | upload |  |
| you asked for a freeze frame or a speed change, which only the fast-montage layout plays | Du wolltest ein Standbild oder ein anderes Tempo, und das spielt nur das Layout der schnellen Montage ab | dondurulmuş kare ya da hız değişimi istedin, bunu sadece hızlı montaj düzeni oynatıyor |  |
| you asked for curved text, which only the fast-montage layout draws | Du wolltest gebogenen Text, und den zeichnet nur das Layout der schnellen Montage | kavisli yazı istedin, bunu sadece hızlı montaj düzeni çiziyor |  |
| you asked for text on individual clips, which only the fast-montage layout places | Du wolltest Text auf einzelnen Clips, und den setzt nur das Layout der schnellen Montage | tek tek kliplere yazı istedin, bunu sadece hızlı montaj düzeni yerleştirebiliyor |  |
| you asked me to keep a part of a clip out, which only the fast-montage layout can guarantee | Du wolltest, dass ich einen Teil eines Clips weglasse, und das kann nur das Layout der schnellen Montage garantieren | bir klibin bir bölümünü dışarıda tutmamı istedin, bunu sadece hızlı montaj düzeni garanti edebiliyor |  |
| you don't say it that many times | so oft sagst du es nicht | bunu o kadar çok kez söylemiyorsun |  |
| you named exact moments, which only the fast-montage layout plays as given | Du hast genaue Momente genannt, und die spielt nur das Layout der schnellen Montage genau so ab | belirli anları sen seçtin, bunları sadece hızlı montaj düzeni olduğu gibi oynatıyor |  |
| you say it in more than one take, so I left it in; tell me which take to cut it from | du sagst es in mehr als einem Take, deshalb habe ich es drin gelassen; sag mir, aus welchem Take ich es schneiden soll | bunu birden fazla çekimde söylüyorsun, o yüzden dokunmadım; hangi çekimden keseyim söyle |  |
| you say it more than once, so tell me which one to cut: the first, second or last | du sagst es mehr als einmal, also sag mir, welches ich schneiden soll: das erste, das zweite oder das letzte | bunu birden fazla kez söylüyorsun, hangisini keseyim söyle: ilkini, ikincisini ya da sonuncusunu |  |
| you set exact timing for photos or cuts, which only the fast-montage layout keeps | Du hast genaue Zeiten für Fotos oder Schnitte festgelegt, und die hält nur das Layout der schnellen Montage ein | fotoğraflar ya da kesimler için net süre verdin, bunu sadece hızlı montaj düzeni koruyor |  |
| your iPhone build can't draw the crossfades that layout uses yet | Deine iPhone-Version kann die Überblendungen dieses Layouts noch nicht zeichnen | iPhone sürümün o düzenin kullandığı geçişleri henüz çizemiyor |  |
| your own song sets the cut points, which only the fast-montage layout follows | Dein eigener Song bestimmt die Schnittpunkte, und denen folgt nur das Layout der schnellen Montage | kendi şarkın kesim noktalarını belirliyor, bunu sadece hızlı montaj düzeni izliyor |  |
| your phone can't draw that layout for this footage | Dein Handy kann dieses Layout für dieses Material nicht zeichnen | telefonun bu görüntüler için o düzeni çizemiyor |  |
| your song sets where the cuts fall | Dein Song bestimmt, wo die Schnitte liegen | kesimlerin yerini şarkın belirliyor |  |
| your take has no word timings to find it in | dein Take hat keine Wortzeiten, in denen ich es finden könnte | çekiminde onu bulacak kelime zamanlaması yok |  |
| your take is {take}s | dein Take ist {take} s lang | çekimin {take} saniye |  |
| {and_names_absent} doesn't appear in the captions, so I couldn't check its spelling. | {and_names_absent} kommt in den Untertiteln nicht vor, deshalb konnte ich die Schreibweise nicht prüfen. | Altyazılarda {and_names_absent} geçmiyor, o yüzden yazımını kontrol edemedim. |  |
| {and_names_absent} don't appear in the captions, so I couldn't check their spelling. | {and_names_absent} kommen in den Untertiteln nicht vor, deshalb konnte ich die Schreibweise nicht prüfen. | Altyazılarda {and_names_absent} geçmiyor, o yüzden yazımlarını kontrol edemedim. |  |
| {and_names_spelled} are spelled as you wrote them. | {and_names_spelled} sind so geschrieben, wie du sie geschrieben hast. | {and_names_spelled} yazdığın gibi yazılmış. |  |
| {and_names_spelled} is spelled as you wrote it. | {and_names_spelled} ist so geschrieben, wie du es geschrieben hast. | {and_names_spelled} yazdığın gibi yazılmış. |  |
| {asked}s | {asked} s | {asked} saniye |  |
| {count} camera effect | {count} Kameraeffekt | {count} {noun_tr} |  |
| {count} camera effects | {count} Kameraeffekte | {count} {noun_tr} |  |
| {count} caption | {count} Untertitel | {count} {noun_tr} |  |
| {count} captions | {count} Untertitel | {count} {noun_tr} |  |
| {count} clip | {count} Clip | {count} {noun_tr} |  |
| {count} clips | {count} Clips | {count} {noun_tr} |  |
| {count} filler sound | {count} Füllgeräusch | {count} dolgu sesi |  |
| {count} filler sounds | {count} Füllgeräusche | {count} dolgu sesi |  |
| {count} label | {count} Label | {count} {noun_tr} |  |
| {count} labels | {count} Labels | {count} {noun_tr} |  |
| {count} long pause | {count} lange Pause | {count} uzun duraklama |  |
| {count} long pauses | {count} lange Pausen | {count} uzun duraklama |  |
| {count} pause | {count} Pause | {count} duraklama |  |
| {count} pauses | {count} Pausen | {count} duraklama |  |
| {count} picture | {count} Bild | {count} {noun_tr} |  |
| {count} pictures | {count} Bilder | {count} {noun_tr} |  |
| {count} restarted sentence | {count} neu begonnener Satz | {count} baştan başlanan cümle |  |
| {count} restarted sentences | {count} neu begonnene Sätze | {count} baştan başlanan cümle |  |
| {count} sound effect | {count} Soundeffekt | {count} {noun_tr} |  |
| {count} sound effects | {count} Soundeffekte | {count} {noun_tr} |  |
| {count} take has no usable singing or words, so it is in as short muted clip. Trim or remove it in the editor. | {count} Take enthält keinen brauchbaren Gesang und keine brauchbaren Worte, deshalb habe ich ihn als kurzen stummen Clip eingefügt. Kürze oder entferne ihn im Editor. | {count} çekimde kullanılabilir şarkı ya da söz yok, bu yüzden kısa ve sessiz klip olarak eklendi. Editörde kısaltabilir veya kaldırabilirsin. |  |
| {count} take is placed by my best guess and may be slightly off; check it in the editor. | {count} Take habe ich nach bester Schätzung platziert, er könnte etwas danebenliegen; prüf ihn im Editor. | {count} çekimi tahminime göre yerleştirdim, biraz kayık olabilir; editörde kontrol et. |  |
| {count} take sits later in the song than a 2-minute video can hold. | {count} Take liegt so spät im Song, dass er nicht mehr in ein 2-Minuten-Video passt. | {count} çekim şarkıda, 2 dakikalık bir videonun sığabileceğinden daha geride kalıyor. |  |
| {count} takes are placed by my best guess and may be slightly off; check them in the editor. | {count} Takes habe ich nach bester Schätzung platziert, sie könnten etwas danebenliegen; prüf sie im Editor. | {count} çekimi tahminime göre yerleştirdim, biraz kayık olabilir; editörde kontrol et. |  |
| {count} takes have no usable singing or words, so they are in as short muted clips. Trim or remove them in the editor. | {count} Takes enthalten keinen brauchbaren Gesang und keine brauchbaren Worte, deshalb habe ich sie als kurze stumme Clips eingefügt. Kürze oder entferne sie im Editor. | {count} çekimde kullanılabilir şarkı ya da söz yok, bu yüzden kısa ve sessiz klip olarak eklendi. Editörde kısaltabilir veya kaldırabilirsin. |  |
| {count} takes sit later in the song than a 2-minute video can hold. | {count} Takes liegen so spät im Song, dass sie nicht mehr in ein 2-Minuten-Video passen. | {count} çekim şarkıda, 2 dakikalık bir videonun sığabileceğinden daha geride kalıyor. |  |
| {count} text | {count} Text | {count} {noun_tr} |  |
| {count} texts | {count} Texte | {count} {noun_tr} |  |
| {label} clips play from {start:.1f} s to {end:.1f} s, under: "{excerpt}" | {label}: Die Clips laufen von {start:.1f} s bis {end:.1f} s, während du sagst: „{excerpt}“ | {tr_label} klipleri {start:.1f} sn ile {end:.1f} sn arasında oynuyor, şu sözlerle: "{excerpt}" |  |
| {len_off} of {len_rows} texts don't have the requested {names} yet | Bei {len_off} von {len_rows} Texten fehlt noch der gewünschte Stil ({names}) | {len_rows} metinden {len_off} tanesinde istenen {names} henüz yok |  |
| {len_off} of {total} text didn't end up reading exactly “{req_literal}”. | Bei {len_off} von {total} Text steht am Ende nicht genau „{req_literal}“. | {total} yazının {len_off} tanesi tam olarak “{req_literal}” olmadı. |  |
| {len_off} of {total} texts didn't end up reading exactly “{req_literal}”. | Bei {len_off} von {total} Texten steht am Ende nicht genau „{req_literal}“. | {total} yazının {len_off} tanesi tam olarak “{req_literal}” olmadı. |  |
| {missed} aren't where you asked. | {missed} sind nicht dort, wo du sie wolltest. | {missed} istediğin yerde değil. |  |
| {missed} isn't where you asked. | {missed} ist nicht dort, wo du es wolltest. | {missed} istediğin yerde değil. |  |
| {more} more | {more} weitere | {more} tane daha |  |
| {names} has no filming time, so I can't put it in the order you filmed. | Bei {names} fehlt die Aufnahmezeit, deshalb kann ich nicht nach der Reihenfolge deiner Aufnahmen sortieren. | {names_tr} için çekim saati yok, o yüzden onu çektiğin sıraya koyamıyorum. |  |
| {names} have no filming time, so I can't put them in the order you filmed. | Bei {names} fehlt die Aufnahmezeit, deshalb kann ich sie nicht in die Reihenfolge deiner Aufnahmen bringen. | {names_tr} için çekim saati yok, o yüzden onları çektiğin sıraya koyamıyorum. |  |
| {n} clip had no capture time, so I kept its attachment order. | {n} Clip hatte keine Aufnahmezeit, deshalb habe ich ihn an der Stelle gelassen, an der du ihn hinzugefügt hast. | {n} klibin çekim zamanı yoktu, o yüzden bunları eklediğin sırada bıraktım. |  |
| {n} clip is too short for its text to stay on screen long enough to read. | {n} Clip ist zu kurz, als dass sein Text lange genug zum Lesen zu sehen wäre. | {n} klip, yazısının okunacak kadar ekranda kalması için çok kısa. |  |
| {n} clips are too short for its text to stay on screen long enough to read. | {n} Clips sind zu kurz, als dass ihr Text lange genug zum Lesen zu sehen wäre. | {n} klip, yazısının okunacak kadar ekranda kalması için çok kısa. |  |
| {n} clips had no capture time, so I kept their attachment order. | {n} Clips hatten keine Aufnahmezeit, deshalb habe ich sie in der Reihenfolge gelassen, in der du sie hinzugefügt hast. | {n} klibin çekim zamanı yoktu, o yüzden bunları eklediğin sırada bıraktım. |  |
| {n} sideways clip | {n} Querformat-Clip | {n} yatay klip |  |
| {n} sideways clips | {n} Querformat-Clips | {n} yatay klip |  |
| {prefix}; {tail} and you asked for {asked} | {prefix}; {tail}, und du wolltest {asked} | {v1}; {tr_tail}, sen {asked_tr} istedin |  |
| {sentence_case_words} is clip {v1} of {len_steps}, not the first one. | {sentence_case_words} ist Clip {v1} von {len_steps}, nicht der erste. | {capitalize_words_strip} {len_steps} klipten {v1}. sırada, {v2} değil. |  |
| {sentence_case_words} is clip {v1} of {len_steps}, not the last one. | {sentence_case_words} ist Clip {v1} von {len_steps}, nicht der letzte. | {capitalize_words_strip} {len_steps} klipten {v1}. sırada, {v2} değil. |  |
| {subject} play at {speed} | {subject} laufen mit {speed} | {subject_tr} {speed} hızda oynuyor |  |
| {subject} play at {speed}, as you asked later | {subject} laufen mit {speed}, wie du es später wolltest | {subject_tr} sonra istediğin gibi {speed} hızda oynuyor |  |
| {subject} play at {speed}, not the {requested:g}x you asked for: a clip plays at least {speed} | {subject} laufen mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft mindestens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en az {speed} hızda oynayabiliyor |  |
| {subject} play at {speed}, not the {requested:g}x you asked for: a clip plays at most {speed} | {subject} laufen mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft höchstens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en fazla {speed} hızda oynayabiliyor |  |
| {subject} play at {speed}, not the {requested:g}x you asked for: the clip ran out of footage | {subject} laufen mit {speed}, nicht mit den gewünschten {requested:g}x: dem Clip ist das Material ausgegangen | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: klibin görüntüsü yetmedi |  |
| {subject} plays at {speed} | {subject} läuft mit {speed} | {subject_tr} {speed} hızda oynuyor |  |
| {subject} plays at {speed}, as you asked later | {subject} läuft mit {speed}, wie du es später wolltest | {subject_tr} sonra istediğin gibi {speed} hızda oynuyor |  |
| {subject} plays at {speed}, not the {requested:g}x you asked for: a clip plays at least {speed} | {subject} läuft mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft mindestens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en az {speed} hızda oynayabiliyor |  |
| {subject} plays at {speed}, not the {requested:g}x you asked for: a clip plays at most {speed} | {subject} läuft mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft höchstens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en fazla {speed} hızda oynayabiliyor |  |
| {subject} plays at {speed}, not the {requested:g}x you asked for: the clip ran out of footage | {subject} läuft mit {speed}, nicht mit den gewünschten {requested:g}x: dem Clip ist das Material ausgegangen | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: klibin görüntüsü yetmedi |  |
| {unnamed} of your recordings never show. | {unnamed} deiner Aufnahmen sind nie zu sehen. | Kayıtlarından {unnamed} tanesi hiç görünmüyor. |  |
| {unnamed} of your recordings never shows. | {unnamed} deiner Aufnahmen ist nie zu sehen. | Kayıtlarından {unnamed} tanesi hiç görünmüyor. |  |
| {v1}; {repeats} more repeated the label before, so I left it off. | {v1}; {repeats} weiterer Clip wiederholte das vorherige Label, deshalb habe ich es dort weggelassen. | {v1}; {repeats} tanesi önceki etiketin aynısıydı, o yüzden koymadım. |  |
| {v1}; {repeats} more repeated the label before, so I left them off. | {v1}; {repeats} weitere Clips wiederholten das vorherige Label, deshalb habe ich es dort weggelassen. | {v1}; {repeats} tanesi önceki etiketin aynısıydı, o yüzden koymadım. |  |
| {words} (clip {n}) | {words} (Clip {n}) | {words} ({n}. klip) |  |
| {words} (first) | {words} (zuerst) | {words} (ilk) |  |
| {words} (last) | {words} (zuletzt) | {words} (son) |  |
| {words} (then) | {words} (danach) | {words} (sonra) |  |
| “{title}” | „{title}“ | “{title}” |  |
| “{title}” by {artist} | „{title}“ von {artist} | “{title}” ({artist}) |  |

### Reworded lines (45)

| English | German (new) | German (before) | OK? |
|---|---|---|---|
| Changed the alignment of {who} | {who}: Ausrichtung geändert | Ausrichtung von {who} geändert |  |
| Changed the colour of {who} | {who}: Farbe geändert | Farbe von {who} geändert |  |
| Changed the entrance animation on {who} | {who}: Eingangsanimation geändert | Eingangsanimation von {who} geändert |  |
| Changed the length of {who} | {who}: Länge geändert | Länge von {who} geändert |  |
| Changed the look of {who} | {who}: Look geändert | Look von {who} geändert |  |
| Changed the source volume of {who} | {who}: Originallautstärke geändert | Originallautstärke von {who} geändert |  |
| Changed the speed of {who} | {who}: Tempo geändert | Tempo von {who} geändert |  |
| Changed the tilt of {who} | {who}: Neigung geändert | Neigung von {who} geändert |  |
| Changed the zoom of {who} | {who}: Zoom geändert | Zoom von {who} geändert |  |
| Changed the {v1} of {who} | {who}: {v1} geändert | {v1} von {who} geändert |  |
| Colour → {shown} on {who} | {who}: Farbe → {shown} | Farbe → {shown} bei {who} |  |
| Cropped the {clips_n} to fill the vertical frame | {clips_n} bildfüllend ins Hochformat zugeschnitten | {clips_n} zugeschnitten, damit sie das Hochformat füllen |  |
| Cropped the {clips_n} to fill the vertical frame{follows} | {clips_n} bildfüllend ins Hochformat zugeschnitten{follows} | {clips_n} zugeschnitten, damit sie das Hochformat füllen{follows} |  |
| Cropped {cropped} of {clips_n} to vertical; kept black bars on {barred} because the face moves too far for one vertical crop to keep it | {cropped} von {clips_n} ins Hochformat zugeschnitten; bei {barred} bleiben schwarze Balken, weil sich das Gesicht zu weit bewegt, als dass ein einziger Hochformat-Ausschnitt es halten könnte | {cropped} von {clips_n} ins Hochformat zugeschnitten; bei {barred} bleiben schwarze Balken, weil sich das Gesicht zu weit bewegt, als dass ein Hochformat-Ausschnitt es halten könnte |  |
| Each {word} plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them. | Jede Gruppe nach „{word}“ läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. | Jede Gruppe ({word}) läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. |  |
| Font → {font_display_first} on {who} | {who}: Schrift → {font_display_first} | Schrift → {font_display_first} bei {who} |  |
| For the {genre} song you asked for, I suggest {song_en}. Add it when you post. | Für den gewünschten Song im Stil {genre} schlage ich dir {song_en} vor. Füg ihn beim Posten hinzu. | Für den gewünschten {genre}-Song schlage ich {song_en} vor. Füg ihn beim Posten hinzu. |  |
| Group by {word}, chronological inside each {word} | Nach „{word}“ gruppieren, chronologisch innerhalb jeder „{word}“-Gruppe | Nach {word} gruppieren, innerhalb jeder Gruppe ({word}) chronologisch |  |
| I can't make this video the length you asked for yet: {instead_en} | Ich kann dieses Video noch nicht in der gewünschten Länge machen: {instead_en} | Ich kann dieses Video noch nicht in der gewünschten Länge erstellen: {instead_en} |  |
| I can't use the {str_value_replace} story archetype on this video yet | Das Story-Muster „{str_value_replace}“ kann ich bei diesem Video noch nicht nutzen | Ich kann das Story-Muster {str_value_replace} in diesem Video noch nicht nutzen |  |
| I can't use the {words_value} story archetype on this video yet | Das Story-Muster „{words_value}“ kann ich bei diesem Video noch nicht nutzen | Ich kann das Story-Muster {words_value} in diesem Video noch nicht nutzen |  |
| I don't have any {join_names} tracks. The closest I have is {song_en}. Add it when you post if you like it. | Ich habe keine Songs im Stil {join_names}. Am nächsten kommt {song_en}. Füg ihn beim Posten hinzu, wenn er dir gefällt. | Ich habe keine {join_names}-Songs. Am nächsten kommt {song_en}. Füg ihn beim Posten hinzu, wenn er dir gefällt. |  |
| I prepared a {pacing} {edit_format} draft around the available footage. | Ich habe aus deinem vorhandenen Material einen Entwurf vorbereitet (Tempo: {pacing}, Format: {edit_format}). | Ich habe einen {pacing}-{edit_format}-Entwurf aus deinem vorhandenen Material vorbereitet. |  |
| Keep it strictly chronological; {word}s may interleave | Streng chronologisch lassen; die Gruppen nach „{word}“ dürfen sich mischen | Streng chronologisch lassen; Gruppen nach {word} dürfen sich mischen |  |
| Kept black bars on the {clips_n}, so nothing is cropped | Schwarze Balken bei {clips_n} beibehalten, also ist nichts abgeschnitten | Schwarze Balken bei {clips_n} beibehalten, damit nichts abgeschnitten wird |  |
| Kria is rendering your video ({phase}). I’ll update this chat when it’s ready. | Kria erstellt gerade dein Video (Schritt: {phase}). Ich melde mich in diesem Chat, sobald es fertig ist. | Kria erstellt gerade dein Video ({phase}). Ich melde mich in diesem Chat, sobald es fertig ist. |  |
| Removed the entrance animation from {who} | {who}: Eingangsanimation entfernt | Eingangsanimation von {who} entfernt |  |
| Reset the zoom of {who} | {who}: Zoom zurückgesetzt | Zoom von {who} zurückgesetzt |  |
| Resized {who} | {who}: Größe geändert | Größe von {who} geändert |  |
| The selected {natural_owner} runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter {natural_owner}. | Die ausgewählte Quelle ({natural_owner}) dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm eine kürzere Quelle ({natural_owner}). | Der ausgewählte {natural_owner} dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm einen kürzeren {natural_owner}. |  |
| The selected {natural_owner} runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Die ausgewählte Quelle ({natural_owner}) dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Der ausgewählte {natural_owner} dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. |  |
| The {str_value_replace} direction isn't applied on this video yet: it is cut as a fast montage | Die Richtung „{str_value_replace}“ wird bei diesem Video noch nicht umgesetzt: Es wird als schnelle Montage geschnitten | Die Richtung {str_value_replace} wird bei diesem Video noch nicht angewendet: Es wird als schnelle Montage geschnitten |  |
| The {str_value_replace} pacing isn't applied on this video yet: the cuts follow a fast pace | Das Tempo „{str_value_replace}“ wird bei diesem Video noch nicht umgesetzt: Die Schnitte folgen einem schnellen Tempo | Das Tempo {str_value_replace} wird bei diesem Video noch nicht angewendet: Die Schnitte folgen einem schnellen Tempo |  |
| The {words_value} direction isn't applied on this video yet: {instead_en} | Die Richtung „{words_value}“ wird bei diesem Video noch nicht umgesetzt: {instead_en} | Die Richtung {words_value} wird bei diesem Video noch nicht angewendet: {instead_en} |  |
| The {words_value} pacing isn't applied on this video yet: {instead_en} | Das Tempo „{words_value}“ wird bei diesem Video noch nicht umgesetzt: {instead_en} | Das Tempo {words_value} wird bei diesem Video noch nicht angewendet: {instead_en} |  |
| You asked for a chronological video and for the clips grouped by {word}. | Du wolltest ein chronologisches Video und die Clips nach „{word}“ gruppiert. | Du wolltest ein chronologisches Video und die Clips nach {word} gruppiert. |  |
| You can tell me which clip shows it, or reply "skip it" and I'll {then}. | Du kannst mir sagen, in welchem Clip es zu sehen ist, oder mit "überspringen" antworten, dann {then}. | Du kannst mir sagen, in welchem Clip es vorkommt, oder "überspringen" antworten, dann {then}. |  |
| You chose black bars, so the {clips_n} keep them | Du hast schwarze Balken gewählt, deshalb bleiben sie bei {clips_n} | Du hast schwarze Balken gewählt, deshalb behalten {clips_n} sie |  |
| You chose crop, so the {clips_n} are cropped to fill the frame | Du hast Zuschneiden gewählt, deshalb habe ich {clips_n} bildfüllend zugeschnitten | Du hast Zuschneiden gewählt, deshalb werden {clips_n} so zugeschnitten, dass sie das Bild füllen |  |
| You're {corner_0} over your background, so you don't fill the frame. | Du bist {corner_0} vor deinem Hintergrund zu sehen, deshalb füllst du das Bild nicht aus. | Du bist {corner_0} über deinem Hintergrund, deshalb füllst du das Bild nicht aus. |  |
| clips stay in the order you filmed them, so· | die Clips bleiben in der Reihenfolge, in der du sie gefilmt hast, deshalb gilt:· | die Clips bleiben in der Reihenfolge, in der du sie gefilmt hast, deshalb· |  |
| you chose strictly chronological order, so· | du hast eine streng chronologische Reihenfolge gewählt, deshalb gilt:· | du hast eine streng chronologische Reihenfolge gewählt, deshalb· |  |
| {candidate_intro} Unfortunately {after_connector_candidate_reason} The most I can do is: {only_label}. Reply "{only_label}" to go with that, or change your request. | {candidate_intro} Leider gibt es einen Haken: {after_connector_candidate_reason} Das Beste, was ich machen kann: {only_label}. Antworte mit "{only_label}", wenn das für dich passt, oder ändere deine Anfrage. | {candidate_intro} Leider {after_connector_candidate_reason} Das Beste, was ich machen kann: {only_label}. Antworte mit "{only_label}", wenn das für dich passt, oder ändere deine Anfrage. |  |
| {candidate_intro} Unfortunately {after_connector_candidate_reason} Which do you prefer? | {candidate_intro} Leider gibt es einen Haken: {after_connector_candidate_reason} Was ist dir lieber? | {candidate_intro} Leider {after_connector_candidate_reason} Was gefällt dir besser? |  |
| {what} is in {count} stretches | {what} liegt in {count} getrennten Abschnitten | {what} ist in {count} Abschnitte geteilt |  |

## Unsure (26 lines)

Lines whose meaning I couldn't pin down from the code; please check these first.

| English key | German draft | Note |
|---|---|---|
| - Left out: {v1} ({omission_reasons_get_req_id}) | - Weggelassen: {v1} ({omission_reasons_get_req_id}) | `{v1}` may be English labels |
| A talk can pause for at most 20 clips, and that is more than that | Deine Rede kann höchstens für 20 Clips pausieren, und das sind mehr | read as: the talk can be interrupted by at most 20 clips |
| Day vlog | Tagesvlog | format name; "Tagesvlog" is a guess |
| I can't add the treatments ({list_items}) on this video yet, so they won't be applied | Ich kann die Effekte ({list_items}) in diesem Video noch nicht hinzufügen, deshalb werden sie nicht angewendet | "treatments" translated as "Effekte" |
| I couldn't tell the {work_intent_attribute} | Ich konnte {work_intent_attribute} nicht erkennen | unclear what the attribute is |
| I need the selected idea's unchanged execution evidence before approval. | Ich brauche die unveränderten Umsetzungsbelege der ausgewählten Idee, bevor du sie freigibst. | technical wording; meaning guessed |
| Keep the video at {exc_minimum_duration_s:g}s | Video bei {exc_minimum_duration_s:g} s lassen | read as an option label |
| Narration labels must be resolved against the recorded voiceover. | Die Erzähl-Labels müssen anhand des aufgenommenen Voiceovers zugeordnet werden. | meaning guessed |
| One render | Ein Video | cost summary on an approval card; read as "one video" |
| Single-hero | Hauptclip | format name; "Hauptclip" is a guess |
| Speech cleanup cut your pauses tight, but | Die Sprachbereinigung hat deine Pausen eng geschnitten, aber | fragment; I don't know what follows "but" |
| The current {current_slots}-slot edit leaves room for only {available} additional slots under the {guided_timeline_max_slots}-slot Save limit, but all {requested} ready unused sources were requested. | Im aktuellen Edit mit {current_slots} Plätzen ist unter dem Speicherlimit von {guided_timeline_max_slots} Plätzen nur noch Platz für {available} weitere, aber es wurden alle {requested} bereiten, unbenutzten Quellen angefragt. | "slot" translated as "Platz" |
| The reviewed story's source windows, wording and sound match its frozen execution. | Die Quellabschnitte, der Wortlaut und der Ton der geprüften Story passen zur festgelegten Umsetzung. | technical wording; meaning guessed |
| The selected footage cannot meet the included-clip constraints. | Das ausgewählte Material kann die Vorgaben für die enthaltenen Clips nicht erfüllen. | meaning guessed |
| Those labels need a recorded voiceover with guided visuals. | Diese Labels brauchen ein aufgenommenes Voiceover mit geführten Visuals. | "guided visuals" translated literally |
| Will do at render | Passiert beim Erstellen des Videos | read as "happens when the video is made" |
| exclude | Weglassen | clip-instruction type shown in a question; noun form is a guess |
| hide_part | Teil ausblenden | clip-instruction type shown in a question; noun form is a guess |
| include | Einbeziehen | clip-instruction type shown in a question; noun form is a guess |
| the complete all-selection can no longer be verified; refresh and try again | die vollständige "Alle"-Auswahl lässt sich nicht mehr prüfen; aktualisiere und versuch es nochmal | "all-selection" read as the "select all" choice |
| the complete all-selection changed after Kria read it; refresh and try again | die vollständige "Alle"-Auswahl hat sich geändert, nachdem Kria sie gelesen hat; aktualisiere und versuch es nochmal | "all-selection" read as the "select all" choice |
| the guided editor revision changed after Kria read it; refresh and try again | die Version des geführten Editors hat sich geändert, nachdem Kria sie gelesen hat; aktualisiere und versuch es nochmal | "guided editor revision" read as a version of the guided editor |
| the saved all-selection proof is incomplete; refresh and try again | der gespeicherte Nachweis der "Alle"-Auswahl ist unvollständig; aktualisiere und versuch es nochmal | "all-selection" read as the "select all" choice |
| the source pool changed after Kria read it; refresh and try again | die Quellen haben sich geändert, nachdem Kria sie gelesen hat; aktualisiere und versuch es nochmal | "source pool" read as "the sources" |
| {label_0} {before}{level_first_after}{unit} | {label_0} {before}{level_first_after}{unit} | kept as is; unclear how the pieces read together |
| {phrase} for {where} | {phrase} ({where}) | could not see what `{phrase}`/`{where}` hold |

## Conflicts (5 keys)

These English keys have different Turkish wordings in different places (`conflicts.json`). German gets one wording per key.

| English key | German | Places (Turkish) | Meaning picked |
|---|---|---|---|
| Updated {v1}. | {v1} aktualisiert. | app/kria/plan_blocks.py:657 "{capitalize_tr} güncelledim."; app/kria/planner.py:6730 "{v1} güncellendi." | Turkish has "I updated X" and "X was updated"; German "{v1} aktualisiert." fits both. |
| caption | Untertitel | app/agents/clip_intent_planner.py:264 "yazı"; app/kria/intent_review_copy.py:22 "altyazı"; app/kria/intent_review_copy.py:22 "Altyazı"; app/services/finished_style_learning.py:626 "altyazı" | Turkish has "yazı" (text on a clip, as a clip-instruction type) and "altyazı" (subtitles); German uses "Untertitel" (subtitles). As a clip-instruction type this reads a bit off. |
| pinned text | angehefteter Text | app/kria/intent_review_copy.py:24 "sabit yazı"; app/kria/intent_review_copy.py:24 "Sabit yazı" | Turkish has mid-sentence and sentence-start forms; German "angehefteter Text" is lowercase, so it reads wrong at the start of a sentence. |
| text | Text | app/kria/intent_review_copy.py:23 "yazı"; app/kria/intent_review_copy.py:23 "Yazı" | Turkish has mid-sentence and sentence-start forms; German "Text" is a noun, so one form fits both. |
| title | Titel | app/kria/intent_review_copy.py:21 "başlık"; app/kria/intent_review_copy.py:21 "Başlık"; app/kria/plan_review.py:251 "başlık"; app/services/finished_style_learning.py:623 "başlık" | Turkish has mid-sentence and sentence-start forms; German "Titel" fits both. |

## All lines (2036)

`·` marks a leading or trailing space. Tick **OK?** when a line is right, or write the fix in `de.json`.

| English | German | Turkish (for context) | OK? |
|---|---|---|---|
| ·(and {extra} more) | ·(und {extra} weitere) | ·(ve {extra} tane daha) |  |
| ·(plus a {round_s_facts_outro_s}s outro on the finished video) | ·(plus ein Outro von {round_s_facts_outro_s} s im fertigen Video) | ·(bitmiş videoda buna ek olarak {round_s_facts_outro_s} saniyelik bir kapanış var) |  |
| ·(recommended) | ·(empfohlen) | ·(önerilen) |  |
| ·Choose a media kind and count of at most {available} while retaining the current edit, or remove existing content first. | ·Wähl eine Medienart und eine Anzahl von höchstens {available}, damit der aktuelle Edit erhalten bleibt, oder entferne zuerst vorhandene Inhalte. | ·Mevcut düzenleme korunarak en fazla {available} olacak şekilde bir medya türü ve sayı seç ya da önce var olan içerikten kaldır. |  |
| ·I'm showing the first {len_offered}. | ·Ich zeige dir die ersten {len_offered}. | ·İlk {len_offered} tanesini gösteriyorum. |  |
| ·If you want a fresh version built from your whole request, reply "redo" and I'll render it again. | ·Wenn du eine neue Version aus deiner ganzen Anfrage möchtest, schreib "nochmal", dann erstelle ich das Video neu. | ·Tüm isteğinden yeni bir sürüm istersen "yeniden yap" yaz, videoyu tekrar oluşturayım. |  |
| ·Please restate just those so I can add them. | ·Schreib mir bitte nur diese nochmal, dann füge ich sie hinzu. | ·Sadece bunları yeniden yazar mısın, ekleyeyim. |  |
| ·Refresh the editor and try again. | ·Lade den Editor neu und versuch es nochmal. | ·Düzenleyiciyi yenileyip tekrar dene. |  |
| ·To change it, reply {join_others}. | ·Zum Ändern schreib {join_others}. | ·Değiştirmek için {join_others_2} yaz. |  |
| ·Your current draft is unchanged. | ·Dein aktueller Entwurf ist unverändert. | ·Mevcut taslağın değişmedi. |  |
| ·and· | ·und· | ·ve· |  |
| ·and {count} more | ·und {count} weitere | ·ve {count} tane daha |  |
| ·and {extra} more | ·und {extra} weitere | ·ve {extra} tane daha |  |
| ·and {hidden} more | ·und {hidden} weitere | ·ve {hidden} tane daha |  |
| ·and {more} more | ·und {more} weitere | ·ve {more} tane daha |  |
| ·or· | ·oder· | ·ya da· |  |
| "{line}" doesn't fit whole in the video's length, so part of it is cut | "{line}" passt nicht ganz in die Länge des Videos, deshalb wird ein Teil abgeschnitten | "{line}" videonun süresine tam sığmıyor, bu yüzden bir kısmı kesiliyor |  |
| "{line}" is said in more than one clip; tell me which clip you mean | "{line}" wird in mehr als einem Clip gesagt; sag mir, welchen Clip du meinst | "{line}" birden fazla klipte söyleniyor; hangi klibi kastettiğini söyle |  |
| "{other}" for {name} | "{other}" statt {name} | {name} yerine "{other}" |  |
| "{quote}" is said too early in that clip to come after your voice | "{quote}" wird in dem Clip zu früh gesagt, um nach deiner Stimme zu kommen | "{quote}" o klipte seslendirmeden sonra gelemeyecek kadar erken söyleniyor |  |
| "{shown}" is too long to fit on one line in a corner (about {pinned_text_max_chars} characters at most). Can you shorten it? | "{shown}" ist zu lang für eine Zeile in der Ecke (höchstens etwa {pinned_text_max_chars} Zeichen). Kannst du es kürzen? | "{shown}" köşede tek satıra sığmayacak kadar uzun (en fazla yaklaşık {pinned_text_max_chars} karakter). Kısaltır mısın? |  |
| "{text}" shows as the opening title for the first few seconds. Talking edits can't keep text on screen the whole time yet. | "{text}" erscheint in den ersten Sekunden als Titel am Anfang. Talking-Edits können Text noch nicht die ganze Zeit auf dem Bildschirm halten. | "{text}" ilk birkaç saniye açılış başlığı olarak görünüyor. Konuşmalı düzenlemeler yazıyı henüz videonun tamamında ekranda tutamıyor. |  |
| "{trigger}" at {at_s:.1f} s | „{trigger}“ bei {at_s:.1f} s | "{trigger}" {at_s:.1f} sn'de |  |
| "{trigger}" is a video, and only a photo or sticker can sit above the title | "{trigger}" ist ein Video, und über dem Titel kann nur ein Foto oder Sticker stehen | "{trigger}" bir video; başlığın üstüne yalnızca fotoğraf ya da çıkartma konur |  |
| , and· | ·und· | ·ve· |  |
| , ending on {pins_last} | , zum Schluss {pins_last} | , {pins_last} ile bitirerek |  |
| , so have a look at the captions | , schau dir also die Untertitel an | , o yüzden altyazılara bir göz at |  |
| , so nothing plays on after your voice | , also läuft nach deiner Stimme nichts weiter | , o yüzden seslendirmeden sonra bir şey çalmıyor |  |
| , starting with {pins_first} | , angefangen mit {pins_first} | , {pins_first} ile başlayarak |  |
| - Left out as agreed: {what} | - Wie besprochen weggelassen: {what} | - Kararlaştırdığımız gibi çıkarıldı: {what} |  |
| - Left out in the simpler version: {what} | - In der einfacheren Version weggelassen: {what} | - Daha basit sürümde çıkarıldı: {what} |  |
| - Left out: {v1} ({omission_reasons_get_req_id}) | - Weggelassen: {v1} ({omission_reasons_get_req_id}) | - Çıkarıldı: {v1} ({omission_reasons_get_req_id}) |  |
| . Could you clarify? | . Kannst du das genauer sagen? | . Biraz açıklar mısın? |  |
| 1 text | 1 Text | 1 yazı |  |
| : tell me the word, or say leave it out | : sag mir das Wort oder sag „weglassen“ | : kelimeyi söyle ya da çıkar de |  |
| ; and more | ; und mehr | ; ve daha fazlası |  |
| ; not shown:· | ; nicht angezeigt:· | ; çıkmadı:· |  |
| ; say if you also want the shadow gone | ; sag Bescheid, wenn auch der Schatten weg soll | ; gölgeyi de kaldırmamı istersen söyle |  |
| ; the crop follows the main face on {faced} | ; der Ausschnitt folgt bei {faced} dem Hauptgesicht | ; {faced} tanesinde kadraj ana yüzü takip ediyor |  |
| A Talking edit keeps only the part of your take you asked for, so its length follows that part | Ein Talking-Edit behält nur den Teil deines Takes, den du wolltest, deshalb richtet sich die Länge nach diesem Teil | Konuşmalı düzenleme çekiminin yalnızca istediğin kısmını korur, o yüzden uzunluğu o kısmı izler |  |
| A Talking edit keeps the whole talking clip, so its length follows it | Ein Talking-Edit behält den ganzen Clip, in dem du sprichst, deshalb richtet sich die Länge danach | Konuşmalı düzenleme konuşma klibini baştan sona korur, o yüzden uzunluğu onu izler |  |
| A Talking edit keeps your whole take, so its length follows your clip | Ein Talking-Edit behält deinen ganzen Take, deshalb richtet sich die Länge nach deinem Clip | Konuşmalı düzenleme çekimini baştan sona korur, o yüzden uzunluğu klibini izler |  |
| A Talking video can't do this, so it stays as filmed | Das geht in einem Talking-Video nicht, deshalb bleibt es wie gefilmt | Konuşmalı videoda bunu yapamıyorum, çekimde olduğu gibi kalır |  |
| A Talking video can't show text at a word you say yet | Ein Talking-Video kann noch keinen Text bei einem Wort zeigen, das du sagst | Konuşmalı video söylediğin bir kelimede yazı gösteremiyor |  |
| A clip can zoom between 0.5x and 3x, so I kept it inside that. | Ein Clip kann zwischen 0,5x und 3x gezoomt werden, deshalb habe ich es in diesem Bereich gehalten. | Bir klip 0,5x ile 3x arasında yakınlaştırılabilir; bu aralığın içinde tuttum. |  |
| A few things didn't match your request: {joined}. I prepared fixes in your draft. Approve them to render, or undo them. | Ein paar Dinge passten nicht zu deiner Anfrage: {joined}. Ich habe Korrekturen in deinem Entwurf vorbereitet. Gib sie frei, um das Video zu erstellen, oder mach sie rückgängig. | Birkaç şey isteğinle uyuşmadı: {joined}. Taslağında düzeltmeler hazırladım. Oluşturmak için onayla ya da geri al. |  |
| A freeze holds the frame where a shot ends, so I can't hold the opening frame, and it plays normally. Tell me the second of the clip to freeze on, like "freeze at 2 seconds" | Ein Standbild hält das Bild am Ende einer Einstellung, deshalb kann ich das erste Bild nicht anhalten, und es läuft normal. Sag mir, bei welcher Sekunde des Clips das Standbild sein soll, z. B. "Standbild bei 2 Sekunden" | Donma bir çekimin bittiği karede olur, o yüzden açılış karesini tutamıyorum ve normal oynuyor. Klibin hangi saniyesinde donsun, yaz; örneğin "2. saniyede dondur" |  |
| A full-screen picture covers the video, so it has no place in front of or behind the pictures on top. | Ein Vollbild-Bild bedeckt das ganze Video, deshalb hat es keinen Platz vor oder hinter den Bildern darüber. | Tam ekran görsel videonun tamamını kaplar; üstteki görsellerin önünde ya da arkasında bir yeri yok. |  |
| A later request changed this | Eine spätere Anfrage hat das geändert | Sonraki bir istek bunu değiştirdi |  |
| A lip-sync edit follows your song, so I'm not timing it to a recorded voiceover. | Ein Lip-Sync-Edit folgt deinem Song, deshalb richte ich ihn nicht nach einem aufgenommenen Voiceover aus. | Dudak senkronlu düzenleme şarkını izler, o yüzden kaydedilmiş seslendirmeye göre zamanlamıyorum. |  |
| A lip-sync edit follows your song, so I'm not using a day-vlog or single-hero shape. | Ein Lip-Sync-Edit folgt deinem Song, deshalb nutze ich keinen Tagesvlog- oder Hauptclip-Aufbau. | Dudak senkronlu düzenleme şarkını izler, o yüzden günlük vlog ya da tek kahraman kurgusu kullanmıyorum. |  |
| A narrated edit is built around your voiceover. Record or import it first, then I'll plan the edit around it. | Ein Narrated-Edit wird um dein Voiceover herum gebaut. Nimm es zuerst auf oder importiere es, dann plane ich den Edit darum herum. | Anlatımlı düzenleme seslendirmen üzerine kurulur. Önce onu kaydet veya içe aktar, sonra düzenlemeyi ona göre planlayayım. |  |
| A photo has no speed to change, so it stays as it is | Ein Foto hat kein Tempo, das sich ändern lässt, deshalb bleibt es, wie es ist | Bir fotoğrafın değişecek hızı yok, o yüzden olduğu gibi kalıyor |  |
| A photo or video you added to Visuals is still being prepared. Give it a moment, then tap Refresh project and I'll start the render. | Ein Foto oder Video, das du zu Visuals hinzugefügt hast, wird noch vorbereitet. Warte einen Moment, tipp dann auf „Refresh project“, und ich starte das Erstellen des Videos. | Visuals bölümüne eklediğin bir fotoğraf ya da video hâlâ hazırlanıyor. Biraz bekle, sonra "Refresh project" düğmesine dokun, videoyu başlatayım. |  |
| A picture behind you is on, which would also cut out the clip | Hinter dir ist ein Bild eingeschaltet, das würde auch den Clip ausschneiden | Arkanda bir resim açık, bu klibi de keserdi |  |
| A picture can't go above the title on this edit yet. | In diesem Edit kann noch kein Bild über dem Titel stehen. | Bu düzenlemede başlığın üstüne henüz görsel konamıyor. |  |
| A requested change is missing. | Eine gewünschte Änderung fehlt. | İstediğin bir değişiklik eksik. |  |
| A slide post plays no added music or voiceover yet, so the planned audio won't be used | Ein Slide-Post spielt noch keine hinzugefügte Musik oder kein Voiceover ab, deshalb wird der geplante Ton nicht verwendet | Slayt gönderisi henüz eklenmiş müzik ya da seslendirme çalmıyor, planlanan ses kullanılmayacak |  |
| A talk can pause for at most 20 clips, and that is more than that | Deine Rede kann höchstens für 20 Clips pausieren, und das sind mehr | Bir konuşma en fazla 20 klip için durabilir, bu ondan fazla |  |
| A tilt is an angle, a zoom, a background, or any mix. I can't read that one. | Eine Neigung ist ein Winkel, ein Zoom, ein Hintergrund oder eine Mischung davon. Das hier verstehe ich nicht. | Eğim bir açı, bir yakınlaştırma, bir arka plan ya da bunların karışımı olabilir. Bunu anlayamadım. |  |
| A tilt is one angle: either the angle to set or the degrees to add, not both. | Eine Neigung ist ein einzelner Winkel: entweder der Winkel, der gesetzt wird, oder die Grad, die dazukommen, nicht beides. | Eğim tek bir açıdır: ya ayarlanacak açı ya da eklenecek derece, ikisi birden değil. |  |
| A tilt needs an angle in degrees, up to a full turn to the left or right. | Für eine Neigung brauche ich einen Winkel in Grad, bis zu einer ganzen Drehung nach links oder rechts. | Eğim için en fazla tam bir tur sola veya sağa olacak şekilde derece cinsinden bir açı gerekiyor. |  |
| A typewriter title can't curve, so it stays straight | Ein Titel im Schreibmaschinen-Stil kann nicht gebogen werden, deshalb bleibt er gerade | Daktilo efektli başlık kavisli olamıyor, o yüzden düz kalıyor |  |
| A vertical crop would put the captions over the speaker's face | Ein Hochformat-Ausschnitt würde die Untertitel über das Gesicht der sprechenden Person legen | Dikey kırpma altyazıları konuşan kişinin yüzünün üstüne getirirdi |  |
| A video of at most {requested:g} seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering. | Ein Video mit höchstens {requested:g} Sekunden kann diese Clips mit ihren freigegebenen Haltezeiten nicht unterbringen. Wähl weniger Clips oder ändere diese Haltezeiten, bevor ich das Video erstelle. | En fazla {requested:g} saniyelik bir video, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir. |  |
| A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered. | Ein Voiceover lässt sich auf deinem iPhone noch nicht erstellen, und die Videos dieses Projekts werden auf diesem iPhone erstellt. Wünsch dir diesen Edit ohne Voiceover. Es wurde kein Ersatz-Edit erstellt. | iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme oluşturulmadı. |  |
| A voiceover edit can use up to 12 of your clips, so I left out the rest. | Ein Voiceover-Edit kann bis zu 12 deiner Clips nutzen, deshalb habe ich den Rest weggelassen. | Seslendirmeli düzenleme kliplerinden en fazla 12 tanesini kullanabiliyor, o yüzden gerisini koymadım. |  |
| A voiceover edit can't show text at a word you say yet. | Ein Voiceover-Edit kann noch keinen Text bei einem Wort zeigen, das du sagst. | Seslendirmeli düzenleme henüz söylediğin bir kelimede yazı gösteremiyor. |  |
| A voiceover edit needs at least one video attached to this project. Add a clip and I'll time it to your voiceover. | Für einen Voiceover-Edit braucht dieses Projekt mindestens ein Video. Füg einen Clip hinzu, dann stimme ich ihn auf dein Voiceover ab. | Seslendirmeli bir düzenleme için projeye en az bir video eklemen lazım. Bir klip ekle, onu seslendirmene göre ayarlarım. |  |
| A voiceover edit runs as long as your voiceover | Ein Voiceover-Edit dauert so lange wie dein Voiceover | Seslendirmeli düzenleme seslendirmen kadar sürer |  |
| A zoom needs a number, like 1.3 for a little closer or 0.8 for a little further. | Für einen Zoom brauche ich eine Zahl, z. B. 1,3 für etwas näher oder 0,8 für etwas weiter weg. | Yakınlaştırma için bir sayı gerekiyor; biraz yakın için 1,3, biraz uzak için 0,8 gibi. |  |
| About when do {shown}{more} appear in your video? Give a rough time for each, or tell me which aren't there. | Wann ungefähr erscheinen {shown}{more} in deinem Video? Nenn mir für jedes eine grobe Zeit oder sag mir, welche nicht vorkommen. | Videonda şunlar yaklaşık ne zaman görünüyor: {shown}{more}? Her biri için yaklaşık bir zaman ver ya da hangilerinin olmadığını söyle. |  |
| About when does "{labels_0}" appear in your video? Give a rough time, such as near the start or 30 seconds, or tell me it isn't there. | Wann ungefähr erscheint "{labels_0}" in deinem Video? Nenn eine grobe Zeit, z. B. ziemlich am Anfang oder bei 30 Sekunden, oder sag mir, dass es nicht vorkommt. | Videonda "{labels_0}" yaklaşık ne zaman görünüyor? Başlara doğru veya 30. saniye gibi yaklaşık bir zaman ver ya da olmadığını söyle. |  |
| Add at least one clip and I can shape the edit around what is actually there. | Füg mindestens einen Clip hinzu, dann kann ich den Edit an dem ausrichten, was wirklich da ist. | En az bir klip ekle, düzenlemeyi gerçekten elindekilere göre şekillendireyim. |  |
| Add or update the selected footage, and I will continue with those clips. | Füg das ausgewählte Material hinzu oder aktualisiere es, dann mache ich mit diesen Clips weiter. | Seçtiğin çekimleri ekle ya da güncelle, ben de o kliplerle devam edeyim. |  |
| Add some footage and I’ll design the first direction. | Füg etwas Material hinzu, dann entwerfe ich die erste Richtung. | Birkaç video ekle, ilk taslağı ben hazırlayayım. |  |
| Add the song you lip-synced to and I'll caption its lyrics. | Füg den Song hinzu, zu dem du Lip-Sync gemacht hast, dann setze ich den Songtext als Untertitel. | Dudak senkronu yaptığın şarkıyı ekle, sözlerini altyazı olarak ekleyeyim. |  |
| Add your song first and I'll use it. | Füg zuerst deinen Song hinzu, dann nutze ich ihn. | Önce şarkını ekle, ben de onu kullanayım. |  |
| Added a shadow to the captions | Schatten zu den Untertiteln hinzugefügt | Altyazılara gölge eklendi |  |
| Added the filming hour to {count} clip. | Aufnahmezeit bei {count} Clip ergänzt. | {count} klibe çekim saatini ekledim. |  |
| Added the filming hour to {count} clips. | Aufnahmezeit bei {count} Clips ergänzt. | {count} klibe çekim saatini ekledim. |  |
| Added {what} | {what} hinzugefügt | {what} eklendi |  |
| Added {who} | {who} hinzugefügt | {who} eklendi |  |
| Adding it would make the video longer than the length you asked for | Wenn ich es hinzufüge, wird das Video länger als gewünscht | Eklersem video istediğin süreden uzun olur |  |
| Adding that media isn't available for on-device edits yet, so I left the video as it was. | Dieses Medium hinzuzufügen ist bei Edits auf dem Gerät noch nicht möglich, deshalb habe ich das Video so gelassen, wie es war. | Bu medyayı eklemek cihaz üzerindeki düzenlemelerde henüz kullanılamıyor, o yüzden videoyu olduğu gibi bıraktım. |  |
| Aiming the recordings isn't available yet, so they're centred. | Die Aufnahmen auszurichten ist noch nicht verfügbar, deshalb sind sie mittig. | Kayıtları istediğin yöne çevirmek henüz kullanılamıyor; şimdilik ortalı duruyorlar. |  |
| Aiming the recordings needs the latest Kria app, so they're centred. | Um die Aufnahmen auszurichten, brauchst du die neueste Kria-App, deshalb sind sie mittig. | Kayıtları istediğin yöne çevirmek için Kria uygulamasının son sürümü gerekiyor; şimdilik ortalı duruyorlar. |  |
| Aligned {who} to the center | {who} zentriert | {who} ortaya hizalandı |  |
| Aligned {who} to the left | {who} linksbündig ausgerichtet | {who} sola hizalandı |  |
| Aligned {who} to the right | {who} rechtsbündig ausgerichtet | {who} sağa hizalandı |  |
| Aligned {who} to the {side} | {who} nach {side} ausgerichtet | {who} {side_tr_get_side_side} hizalandı |  |
| All selected text already has that appearance; no changes were staged. | Alle ausgewählten Texte sehen schon so aus; es wurde nichts geändert. | Seçili yazıların hepsi zaten bu görünümde; hiçbir şey değişmedi. |  |
| All your clips are already vertical, so they fill the frame | Alle deine Clips sind schon im Hochformat, deshalb füllen sie das Bild | Kliplerinin hepsi zaten dikey, o yüzden ekranı dolduruyorlar |  |
| Also changed:· | Außerdem geändert:· | Ayrıca değişti:· |  |
| Also not applied on this video yet: | Auch in diesem Video noch nicht umgesetzt: | Bu videoda henüz uygulanmayanlar: |  |
| Also: | Außerdem: | Ayrıca: |  |
| An uploaded visual is no longer available in this draft | Ein hochgeladenes Bild ist in diesem Entwurf nicht mehr verfügbar | Yüklediğin bir görsel artık bu taslakta yok |  |
| Analyzing your footage didn't finish. Try again. | Die Analyse deines Materials wurde nicht fertig. Versuch es nochmal. | Çekimlerinin analizi tamamlanmadı. Tekrar dene. |  |
| Another label starts at the same moment, so this one isn't shown | Ein anderes Label beginnt im selben Moment, deshalb wird dieses nicht angezeigt | Aynı anda başka bir etiket başlıyor, bu yüzden bu gösterilmedi |  |
| Another request for the end of the video took its place | Eine andere Anfrage für das Ende des Videos hat seinen Platz eingenommen | Videonun sonu için başka bir istek onun yerini aldı |  |
| Answer both and I'll plan your edit. | Beantworte beides, dann plane ich deinen Edit. | İkisini de yanıtla, düzenlemeni planlayayım. |  |
| Applied noise reduction; engine and speech are not independently isolated | Rauschunterdrückung angewendet; Motor und Sprache sind nicht unabhängig voneinander getrennt | Gürültü azaltma uygulandı; motor sesiyle konuşma birbirinden bağımsız ayrılmadı |  |
| Applied the speech cut | Sprachschnitt angewendet | Konuşma kesimi uygulandı |  |
| Applied your edit | Deine Änderung übernommen | Düzenlemeni uyguladım |  |
| Arabic | Arabisch |  |  |
| As agreed earlier: {note} | Wie vorher besprochen: {note} | Daha önce anlaştığımız gibi: {note} |  |
| As you asked, I dropped "{words}" | Wie gewünscht habe ich "{words}" weggelassen | İstediğin gibi "{words}" kısmını çıkardım |  |
| As you chose | Wie du es gewählt hast | Senin seçimin |  |
| Ask for a different format. | Wünsch dir ein anderes Format. | Farklı bir format iste. |  |
| Ask for a montage with your voice, or a different format. | Wünsch dir eine Montage mit deiner Stimme oder ein anderes Format. | Sesinle bir montaj ya da farklı bir format iste. |  |
| Ask for a montage, a subtitled clip, or a narrated edit. | Wünsch dir eine Montage, einen Clip mit Untertiteln oder einen Narrated-Edit. | Bir montaj, altyazılı bir klip ya da seslendirmeli bir düzenleme iste. |  |
| Ask for a spoken-excerpt montage on your iPhone, which can keep only that camera audio, or tell me to drop that requirement. | Wünsch dir eine Montage aus gesprochenen Ausschnitten auf deinem iPhone, die nur diesen Kameraton behalten kann, oder sag mir, dass ich diese Vorgabe weglassen soll. | iPhone'unda yalnızca o kamera sesini koruyabilen, konuşma kesitli bir montaj iste ya da bu isteği bırakmamı söyle. |  |
| Ask for a spoken-excerpt montage on your iPhone, which keeps or mutes camera audio as you confirmed, or tell me to drop that requirement. | Wünsch dir eine Montage aus gesprochenen Ausschnitten auf deinem iPhone, die den Kameraton so behält oder stummschaltet, wie du es bestätigt hast, oder sag mir, dass ich diese Vorgabe weglassen soll. | iPhone'unda kamera sesini onayladığın gibi koruyan ya da kısan, konuşma kesitli bir montaj iste ya da bu isteği bırakmamı söyle. |  |
| Ask for it to be rendered on your iPhone, where I can check it, or tell me to drop that requirement. | Lass das Video auf deinem iPhone erstellen, dort kann ich es prüfen, oder sag mir, dass ich diese Vorgabe weglassen soll. | Kontrol edebilmem için iPhone'unda oluşturulmasını iste ya da bu isteği bırakmamı söyle. |  |
| Ask for it to be rendered on your iPhone, which renders clips in the order you confirmed, or tell me to drop that requirement. | Lass das Video auf deinem iPhone erstellen, das die Clips in der bestätigten Reihenfolge erstellt, oder sag mir, dass ich diese Vorgabe weglassen soll. | Klipleri onayladığın sırayla oluşturan iPhone'unda oluşturulmasını iste ya da bu isteği bırakmamı söyle. |  |
| Ask me to make the change as a new edit instead. | Bitte mich stattdessen, die Änderung als neuen Edit zu machen. | Bunun yerine değişikliği yeni bir düzenleme olarak yapmamı iste. |  |
| Ask me to make the edit again from your latest request. | Bitte mich, den Edit nach deiner letzten Anfrage neu zu machen. | Düzenlemeyi son isteğine göre yeniden yapmamı iste. |  |
| Automatic music matching is turned off right now. Try again later. | Die automatische Musikauswahl ist gerade ausgeschaltet. Versuch es später nochmal. | Otomatik müzik eşleştirme şu anda kapalı. Daha sonra tekrar dene. |  |
| Background | Hintergrund | Arka plan |  |
| Balanced pacing | Tempo: Ausgewogen | Tempo: Dengeli |  |
| Before approval, this edit format cannot prepare sourced research text. I can show you sourced facts in a standard montage instead, or keep this format without the facts if you choose. | Vor der Freigabe kann dieses Edit-Format keinen Text mit belegten Recherche-Fakten vorbereiten. Ich kann dir die belegten Fakten stattdessen in einer normalen Montage zeigen oder dieses Format ohne die Fakten behalten, wie du möchtest. | Bu kurgu türü onaydan önce kaynaklı araştırma metni hazırlayamıyor. İstersen kaynaklı bilgileri standart bir montajda gösterebilirim ya da bu türü bilgiler olmadan koruyabilirim. |  |
| Behind a tilted clip I can show a plain background, a blurred copy of the clip, one colour (like #112233), or a photo or video from this video's Visuals. | Hinter einem geneigten Clip kann ich einen einfachen Hintergrund, eine unscharfe Kopie des Clips, eine einzelne Farbe (z. B. #112233) oder ein Foto oder Video aus den Visuals dieses Videos zeigen. | Eğik klibin arkasında düz bir arka plan, klibin bulanık bir kopyası, tek bir renk (örneğin #112233) ya da bu videonun görsellerinden bir fotoğraf ya da video gösterebilirim. |  |
| Both listens heard the same words, so I didn't find an unclear word | Beide Durchgänge haben dieselben Wörter verstanden, deshalb habe ich kein unklares Wort gefunden | İki dinleme de aynı kelimeleri duydu, o yüzden belirsiz bir kelime bulmadım |  |
| Cancelled videos cannot be edited. | Abgebrochene Videos lassen sich nicht bearbeiten. | İptal edilen videolar düzenlenemez. |  |
| Captions are off in this draft. | Die Untertitel sind in diesem Entwurf aus. | Bu taslakta altyazılar kapalı. |  |
| Captions are on as full sentences, not word by word. | Die Untertitel sind an, als ganze Sätze und nicht Wort für Wort. | Altyazılar kelime kelime değil, tam cümleler olarak açık. |  |
| Captions are on in this video. | Die Untertitel sind in diesem Video an. | Bu videoda altyazılar açık. |  |
| Captions are still on in this draft. | Die Untertitel sind in diesem Entwurf noch an. | Bu taslakta altyazılar hâlâ açık. |  |
| Captions now enter with {vocab_label} | Untertitel erscheinen jetzt mit {vocab_label} | Altyazılar artık {vocab_label_tr} ile giriyor |  |
| Captions now light up word by word | Untertitel leuchten jetzt Wort für Wort auf | Altyazılar artık kelime kelime vurgulanıyor |  |
| Captions now show whole lines | Untertitel zeigen jetzt ganze Zeilen | Altyazılar artık tam satır görünüyor |  |
| Change it | Ändern | Değiştir |  |
| Change it to {quoted} | In {quoted} ändern | {quoted} olarak değiştir |  |
| Change my request | Meine Anfrage ändern | İsteğimi değiştir |  |
| Changed how sideways clips fill the frame | Geändert, wie seitlich gedrehte Clips das Bild füllen | Yan çekilmiş kliplerin kareyi doldurma biçimi değişti |  |
| Changed the Bend of {who} | {who}: Biegung geändert | {who} için Kavis değişti |  |
| Changed the alignment of {who} | {who}: Ausrichtung geändert | {who} hizalaması değişti |  |
| Changed the animation speed of {who} | {who}: Animationstempo geändert | {who} için animasyon hızı değişti |  |
| Changed the background behind {who}: blurred background | {who}: unscharfer Hintergrund | {who} arka planı değişti: bulanık arka plan |  |
| Changed the background behind {who}: photo or video behind it | {who}: Foto oder Video als Hintergrund | {who} arka planı değişti: arkasında fotoğraf ya da video |  |
| Changed the background behind {who}: plain | {who}: schlichter Hintergrund | {who} arka planı değişti: düz |  |
| Changed the background behind {who}: {kind} background | {who}: Hintergrund {kind} | {who} arka planı değişti: {kind} arka plan |  |
| Changed the background of {who} | {who}: Hintergrund geändert | {who} için arka plan değişti |  |
| Changed the behind-subject look of {who} | {who}: Hinter-dem-Motiv-Effekt geändert | {who} için özne arkası görünümü değişti |  |
| Changed the caption animation | Untertitel-Animation geändert | Altyazı animasyonu değişti |  |
| Changed the caption colour | Untertitel-Farbe geändert | Altyazı rengi değişti |  |
| Changed the caption font to {after} | Untertitel-Schrift auf {after} geändert | Altyazı yazı tipi {after} oldu |  |
| Changed the caption highlight colour | Hervorhebungsfarbe der Untertitel geändert | Altyazı vurgu rengi değişti |  |
| Changed the caption outline | Untertitel-Kontur geändert | Altyazı konturu değişti |  |
| Changed the caption size | Untertitel-Größe geändert | Altyazı boyutu değişti |  |
| Changed the colour of {who} | {who}: Farbe geändert | {who} rengi değişti |  |
| Changed the entrance animation on {who} | {who}: Eingangsanimation geändert | {who} için giriş animasyonu değişti |  |
| Changed the exit animation of {who} | {who}: Ausgangsanimation geändert | {who} için çıkış animasyonu değişti |  |
| Changed the highlight colour of {who} | {who}: Hervorhebungsfarbe geändert | {who} için vurgu rengi değişti |  |
| Changed the length of {who} | {who}: Länge geändert | {who} süresi değişti |  |
| Changed the letter spacing of {who} | {who}: Zeichenabstand geändert | {who} için harf aralığı değişti |  |
| Changed the line spacing of {who} | {who}: Zeilenabstand geändert | {who} için satır aralığı değişti |  |
| Changed the look of {who} | {who}: Look geändert | {who} görünümü değişti |  |
| Changed the looping animation of {who} | {who}: Schleifenanimation geändert | {who} için döngü animasyonu değişti |  |
| Changed the music | Musik geändert | Müzik değişti |  |
| Changed the outline of {who} | {who}: Kontur geändert | {who} için kontur değişti |  |
| Changed the rotation of {who} | {who}: Drehung geändert | {who} için dönüş değişti |  |
| Changed the shadow of {who} | {who}: Schatten geändert | {who} için gölge değişti |  |
| Changed the source volume of {who} | {who}: Originallautstärke geändert | {who} kaynak ses düzeyi değişti |  |
| Changed the speed of {who} | {who}: Tempo geändert | {who} hızı değişti |  |
| Changed the text width of {who} | {who}: Textbreite geändert | {who} için yazı genişliği değişti |  |
| Changed the tilt of {who} | {who}: Neigung geändert | {who} eğimi değişti |  |
| Changed the video's shape | Das Format des Videos geändert | Videonun biçimi değişti |  |
| Changed the volume of your song | Die Lautstärke deines Songs geändert | Şarkının ses düzeyi değişti |  |
| Changed the zoom of {who} | {who}: Zoom geändert | {who} yakınlaştırması değişti |  |
| Changed the {v1} of {who} | {who}: {v1} geändert | {who} için {v2} değişti |  |
| Changed where the captions sit | Die Position der Untertitel geändert | Altyazıların yeri değişti |  |
| Changed where your song plays | Geändert, welcher Teil deines Songs läuft | Şarkının çalan kısmı değişti |  |
| Changed which picture is on top | Geändert, welches Bild oben liegt | Hangi görselin üstte olduğu değişti |  |
| Changed {name_replace} | {name_replace} geändert | {name_replace} değişti |  |
| Changed {v1} to {v2} | {v1} in {v2} geändert | {v1} yazısını {v2} yaptım |  |
| Changed {what} | {what} geändert | {what} değişti |  |
| Changing the wording needs your confirmation. Should I keep the exact original words and adjust only their timing? | Den Wortlaut zu ändern braucht deine Bestätigung. Soll ich die Original-Wörter genau so lassen und nur das Timing anpassen? | Metni değiştirmek için onayın gerekiyor. Aynı kelimeleri koruyup yalnızca zamanlamalarını ayarlayayım mı? |  |
| Check before recording, I couldn't confirm these from your clips: {listed}. | Prüf das vor der Aufnahme, ich konnte das anhand deiner Clips nicht bestätigen: {listed}. | Kaydetmeden önce kontrol et, bunları kliplerinden doğrulayamadım: {listed}. |  |
| Checked in the video. | Im Video geprüft. | Videoda kontrol edildi. |  |
| Choose Clean up speech when you approve and every pause is cut down to about 0.3 s | Wähl beim Freigeben „Clean up speech“, dann wird jede Pause auf etwa 0,3 s gekürzt | Onaylarken “Konuşmayı temizle” seçeneğini seç, her duraklama yaklaşık 0,3 sn'ye kadar kısalır |  |
| Choose Clean up speech when you approve and the long pauses are cut | Wähl beim Freigeben „Clean up speech“, dann werden die langen Pausen herausgeschnitten | Onaylarken “Konuşmayı temizle” seçeneğini seç, uzun duraklamalar kesilir |  |
| Choose a format and I’ll shape the edit around it. | Wähl ein Format, dann gestalte ich den Edit danach. | Bir format seç, kurguyu ona göre şekillendireyim. |  |
| Choose a shorter part or allow a longer video. | Wähl einen kürzeren Teil oder erlaube ein längeres Video. | Daha kısa bir bölüm seç veya daha uzun bir videoya izin ver. |  |
| Choose another song or render with footage audio. | Wähl einen anderen Song oder lass das Video mit dem Ton aus deinem Material erstellen. | Başka bir şarkı seç ya da videoyu çekimlerin kendi sesiyle oluştur. |  |
| Choose different clips | Andere Clips wählen | Farklı klipler seç |  |
| Choose either your recorded voice or the camera audio for this edit. | Wähl für diesen Edit entweder deine aufgenommene Stimme oder den Kameraton. | Bu düzenleme için ya kaydettiğin sesi ya da kamera sesini seç. |  |
| Choose how to handle the pauses and filler words before I render. | Wähl, wie ich mit den Pausen und Füllwörtern umgehen soll, bevor ich das Video erstelle. | Videoyu oluşturmadan önce duraklamaların ve dolgu sözcüklerinin nasıl ele alınacağını seç. |  |
| Choose one soundtrack: the camera audio, your voice, or your song. | Wähl einen einzigen Ton: den Kameraton, deine Stimme oder deinen Song. | Tek bir ses seç: kamera sesi, kendi sesin ya da şarkın. |  |
| Choose one speaker clip before I cut that phrase | Wähl einen Sprech-Clip, bevor ich diese Stelle schneide | Bu ifadeyi kesmeden önce tek bir konuşmacı klibi seç |  |
| Choose which parts of the video to change. | Wähl, welche Teile des Videos ich ändern soll. | Videonun hangi bölümlerini değiştireceğimizi seç. |  |
| Clean up speech also cuts a sentence you restarted if it finds one, and I'll tell you after | „Clean up speech“ schneidet auch einen Satz heraus, den du neu angefangen hast, falls es einen findet, und ich sag dir danach Bescheid | “Konuşmayı temizle” baştan başladığın bir cümle bulursa onu da keser ve sonra sana söylerim |  |
| Clean up speech looks only for pauses and filler sounds, so a sentence you restarted stays in | „Clean up speech“ sucht nur nach Pausen und Fülllauten, deshalb bleibt ein neu angefangener Satz drin | “Konuşmayı temizle” yalnızca duraklamaları ve dolgu seslerini arar, o yüzden baştan başladığın bir cümle videoda kalır |  |
| Cleaning up speech isn't available for this iPhone edit yet. Tap Refresh project and choose to keep the original speech. | Die Sprachbereinigung ist für diesen iPhone-Edit noch nicht verfügbar. Tipp auf „Refresh project“ und wähl, die Originalsprache zu behalten. | Konuşma temizleme bu iPhone düzenlemesi için henüz kullanılamıyor. "Refresh project" düğmesine dokun ve özgün konuşmayı korumayı seç. |  |
| Cleaning up the audio didn't finish. Try again, or turn off speech cleanup. | Die Tonbereinigung wurde nicht fertig. Versuch es nochmal oder schalte die Sprachbereinigung aus. | Sesi temizleme tamamlanmadı. Tekrar dene ya da konuşma temizlemeyi kapat. |  |
| Clip {index} | Clip {index} | {index}. klip |  |
| Clip {names} doesn't look like "{words}" from what I saw, so I didn't put that wording on it. Tell me what it is, or ask me to describe what I see. | Clip {names} sieht nach dem, was ich gesehen habe, nicht nach "{words}" aus, deshalb habe ich diesen Text nicht daraufgesetzt. Sag mir, was darin zu sehen ist, oder bitte mich, zu beschreiben, was ich sehe. | Klip {names} gördüğüm kadarıyla "{words}" gibi görünmüyor, o yüzden bu ifadeyi koymadım. Ne olduğunu söyle ya da gördüğümü anlatmamı iste. |  |
| Clip {number} isn't in this video, so nothing plays at a different speed | Clip {number} ist nicht in diesem Video, deshalb läuft nichts in einem anderen Tempo | {number}. klip bu videoda yok, bu yüzden hiçbir yerin hızı değişmiyor |  |
| Clip {n} | Clip {n} | {n}. klip |  |
| Clip {which} can't be in this edit. | Clip {which} kann nicht in diesem Edit sein. | {which}. klip bu düzenlemeye giremiyor. |  |
| Clips play in the exact order you filmed them. | Die Clips laufen genau in der Reihenfolge, in der du sie gefilmt hast. | Klipler tam olarak çektiğin sırayla oynar. |  |
| Clips shown full screen play their own sound, with your voice muted under them | Clips im Vollbild spielen ihren eigenen Ton, deine Stimme ist darunter stummgeschaltet | Tam ekran gösterilen klipler kendi sesiyle çalıyor, senin sesin altta kapalı |  |
| Clips shown full screen where you talk about them play their own sound, with your voice muted under them. A clip nothing you say matches stays silent, and I'll tell you which | Clips, die im Vollbild gezeigt werden, wenn du über sie sprichst, spielen ihren eigenen Ton, deine Stimme ist darunter stummgeschaltet. Ein Clip, zu dem nichts passt, was du sagst, bleibt stumm, und ich sag dir, welcher | Hakkında konuştuğun yerde tam ekran gösterilen klipler kendi sesiyle çalar, senin sesin altta kapalı kalır. Anlattığın hiçbir şeyle eşleşmeyen klip sessiz kalır, hangisi olduğunu söylerim |  |
| Clips without one stay where they are. | Clips ohne Aufnahmezeit bleiben, wo sie sind. | Çekim saati olmayan klipler yerinde kalıyor. |  |
| Clips {head} and {last} | Clips {head} und {last} | {v1} klipler |  |
| Clips {names} don't look like "{words}" from what I saw, so I didn't put that wording on them. Tell me what they are, or ask me to describe what I see. | Die Clips {names} sehen nach dem, was ich gesehen habe, nicht nach "{words}" aus, deshalb habe ich diesen Text nicht daraufgesetzt. Sag mir, was darin zu sehen ist, oder bitte mich, zu beschreiben, was ich sehe. | Klip {names} gördüğüm kadarıyla "{words}" gibi görünmüyor, o yüzden bu ifadeyi koymadım. Ne olduğunu söyle ya da gördüğümü anlatmamı iste. |  |
| Clips {which} can't be in this edit. | Die Clips {which} können nicht in diesem Edit sein. | {which}. klipler bu düzenlemeye giremiyor. |  |
| Colour → {shown} on {who} | {who}: Farbe → {shown} | {who} için renk → {shown} |  |
| Continue without a fixed order | Ohne feste Reihenfolge weiter | Sabit bir sıra olmadan devam et |  |
| Continue without a title | Ohne Titel weiter | Başlıksız devam et |  |
| Couldn't | Ging nicht | Yapamadım |  |
| Couldn't add that at the end with its own sound: it's a photo, not a video. A photo can be the closing photo instead. | Konnte das nicht mit eigenem Ton ans Ende setzen: Es ist ein Foto, kein Video. Ein Foto kann stattdessen das Schlussfoto sein. | Bunu kendi sesiyle sona ekleyemedim: bu bir fotoğraf, video değil. Fotoğraf kapanış fotoğrafı olarak girebilir. |  |
| Couldn't add that at the end with its own sound: this iPhone can't mix its sound into the video yet. | Konnte das nicht mit eigenem Ton ans Ende setzen: Dieses iPhone kann seinen Ton noch nicht ins Video mischen. | Bunu kendi sesiyle sona ekleyemedim: bu iPhone sesini videoya karıştıramıyor henüz. |  |
| Couldn't add that clip at the end silent: one of your own clips can only end the video with its own sound. | Konnte diesen Clip nicht stumm ans Ende setzen: Ein eigener Clip kann das Video nur mit seinem eigenen Ton beenden. | Bu klibi sessiz olarak sona ekleyemedim: kendi kliplerinden biri videoyu ancak kendi sesiyle bitirebilir. |  |
| Couldn't add that clip at the end: ending on another of your clips needs a Talking edit with several clips, which isn't available yet. | Konnte diesen Clip nicht ans Ende setzen: Um mit einem anderen deiner Clips zu enden, braucht es einen Talking-Edit mit mehreren Clips, und den gibt es noch nicht. | Bu klibi sona ekleyemedim: başka bir klibinle bitirmek için birden çok klipli konuşmalı düzenleme gerekiyor ve o henüz yok. |  |
| Couldn't add that clip at the end: it is the only clip in this edit. | Konnte diesen Clip nicht ans Ende setzen: Er ist der einzige Clip in diesem Edit. | Bu klibi sona ekleyemedim: bu düzenlemedeki tek klip bu. |  |
| Couldn't add that clip at the end: you asked to leave it out of this edit. | Konnte diesen Clip nicht ans Ende setzen: Du wolltest ihn aus diesem Edit weglassen. | Bu klibi sona ekleyemedim: bu düzenlemeden çıkarmamı istemiştin. |  |
| Couldn't add that video at the end with its own sound: only a talking-to-camera edit on your iPhone can end on another video. | Konnte dieses Video nicht mit eigenem Ton ans Ende setzen: Nur ein Edit auf deinem iPhone, in dem du in die Kamera sprichst, kann mit einem anderen Video enden. | Bu videoyu kendi sesiyle sona ekleyemedim: sadece iPhone'daki konuşmalı düzenlemeler başka bir videoyla bitebilir. |  |
| Couldn't add that video at the end: this iPhone can't play videos from Visuals yet. | Konnte dieses Video nicht ans Ende setzen: Dieses iPhone kann Videos aus Visuals noch nicht abspielen. | Bu videoyu sona ekleyemedim: bu iPhone Visuals'taki videoları henüz oynatamıyor. |  |
| Couldn't add that video at the end: this iPhone render can't place pictures and videos around your talk yet. | Konnte dieses Video nicht ans Ende setzen: Das Erstellen auf diesem iPhone kann Bilder und Videos noch nicht um deine Rede herum platzieren. | Bu videoyu sona ekleyemedim: bu iPhone oluşturması konuşmanın çevresine resim ve video koyamıyor henüz. |  |
| Couldn't apply engine noise reduction | Motorgeräusch-Reduzierung ging nicht | Motor gürültüsünü azaltamadım |  |
| Couldn't change the backgrounds or put you small in a corner: your iPhone app can't switch backgrounds yet. One background stays behind you for the whole video. | Konnte die Hintergründe nicht wechseln und dich nicht klein in eine Ecke setzen: Deine iPhone-App kann Hintergründe noch nicht wechseln. Ein Hintergrund bleibt das ganze Video über hinter dir. | Arka planları değiştirip seni köşede küçük gösteremedim: iPhone uygulaman henüz arka plan değiştiremiyor. Bütün video boyunca arkanda tek bir arka plan duruyor. |  |
| Couldn't find "{name}"'s photo/sticker; left it out. | Konnte das Foto oder den Sticker für "{name}" nicht finden; habe es weggelassen. | "{name}" için fotoğraf ya da çıkartma bulamadım; koymadım. |  |
| Couldn't find some of the photos or videos you named for the changing background; left them out. | Konnte einige der genannten Fotos oder Videos für den wechselnden Hintergrund nicht finden; habe sie weggelassen. | Değişen arka plan için söylediğin bazı fotoğraf ya da videoları bulamadım; onları çıkardım. |  |
| Couldn't find the clip the small card should follow; left that card out. | Konnte den Clip nicht finden, dem die kleine Karte folgen soll; habe die Karte weggelassen. | Küçük kartın takip edeceği klibi bulamadım; o kartı koymadım. |  |
| Couldn't find the closing badge you named; left it off. | Konnte das genannte Abschluss-Badge nicht finden; habe es weggelassen. | Söylediğin kapanış rozetini bulamadım; koymadım. |  |
| Couldn't find the closing photo you named; kept the normal ending. | Konnte das genannte Schlussfoto nicht finden; das normale Ende bleibt. | Söylediğin kapanış fotoğrafını bulamadım; normal bitişi bıraktım. |  |
| Couldn't find the photo or video you named for a small card; left it out. | Konnte das genannte Foto oder Video für eine kleine Karte nicht finden; habe es weggelassen. | Küçük kart için söylediğin fotoğrafı ya da videoyu bulamadım; koymadım. |  |
| Couldn't find the photo or video you named for the background; kept your own background. | Konnte das genannte Foto oder Video für den Hintergrund nicht finden; dein eigener Hintergrund bleibt. | Arka plan için söylediğin fotoğraf ya da videoyu bulamadım; kendi arka planını bıraktım. |  |
| Couldn't find the video you named for the end; kept the normal ending. | Konnte das genannte Video für das Ende nicht finden; das normale Ende bleibt. | Son için söylediğin videoyu bulamadım; normal bitişi bıraktım. |  |
| Couldn't put a plain colour behind you: your iPhone app can't show one yet; kept your own background. | Konnte keine einfarbige Fläche hinter dich legen: Deine iPhone-App kann das noch nicht anzeigen; dein eigener Hintergrund bleibt. | Arkana düz bir renk koyamadım: iPhone uygulaman henüz bunu gösteremiyor; kendi arka planını bıraktım. |  |
| Couldn't put that behind you: a photo or video background only works on a talking-to-camera edit rendered on your iPhone, and it isn't available here right now. | Konnte das nicht hinter dich legen: Ein Foto- oder Video-Hintergrund geht nur bei einem Edit, in dem du in die Kamera sprichst und der auf deinem iPhone erstellt wird, und das ist hier gerade nicht verfügbar. | Bunu arkana koyamadım: fotoğraf ya da video arka planı sadece iPhone'da hazırlanan konuşmalı düzenlemelerde çalışıyor ve şu an burada kullanılamıyor. |  |
| Couldn't start you full screen before the background: it isn't available here right now. | Konnte dich nicht im Vollbild vor dem Hintergrund starten lassen: Das ist hier gerade nicht verfügbar. | Arka plandan önce seni tam ekran başlatamadım: bu şu an burada kullanılamıyor. |  |
| Couldn't start you full screen before the background: your iPhone app can't do that yet. Update the app and ask again. | Konnte dich nicht im Vollbild vor dem Hintergrund starten lassen: Deine iPhone-App kann das noch nicht. Aktualisiere die App und frag nochmal. | Arka plandan önce seni tam ekran başlatamadım: iPhone uygulaman bunu henüz yapamıyor. Uygulamayı güncelleyip tekrar iste. |  |
| Couldn't use that as the background: this iPhone can't draw it behind you yet. | Konnte das nicht als Hintergrund nutzen: Dieses iPhone kann es noch nicht hinter dir zeigen. | Bunu arka plan yapamadım: bu iPhone onu henüz arkanda çizemiyor. |  |
| Couldn't use that colour for your background; kept your own background. Try white, black, green, blue, red, yellow or grey. | Konnte diese Farbe nicht für deinen Hintergrund nutzen; dein eigener Hintergrund bleibt. Probier Weiß, Schwarz, Grün, Blau, Rot, Gelb oder Grau. | Bu rengi arka planın için kullanamadım; kendi arka planını bıraktım. Beyaz, siyah, yeşil, mavi, kırmızı, sarı ya da gri dene. |  |
| Couldn't use the background you picked in the camera: it is no longer in this edit's photos and videos; kept your own background. | Konnte den Hintergrund, den du in der Kamera gewählt hast, nicht nutzen: Er gehört nicht mehr zu den Fotos und Videos dieses Edits; dein eigener Hintergrund bleibt. | Kamerada seçtiğin arka planı kullanamadım: artık bu düzenlemenin fotoğraf ve videoları arasında değil; kendi arka planını bıraktım. |  |
| Couldn't verify that exact spoken phrase occurrence was cut | Ich konnte nicht bestätigen, dass genau diese gesprochene Stelle herausgeschnitten wurde | O konuşulan ifadenin tam o tekrarının kesildiğini doğrulayamadım |  |
| Cropped the {clips_n} to fill the vertical frame | {clips_n} bildfüllend ins Hochformat zugeschnitten | {clips_n} dikey ekranı dolduracak şekilde kırpıldı |  |
| Cropped the {clips_n} to fill the vertical frame{follows} | {clips_n} bildfüllend ins Hochformat zugeschnitten{follows} | {clips_n} dikey ekranı dolduracak şekilde kırpıldı{follows} |  |
| Cropped {cropped} of {clips_n} to vertical; kept black bars on {barred} because the face moves too far for one vertical crop to keep it | {cropped} von {clips_n} ins Hochformat zugeschnitten; bei {barred} bleiben schwarze Balken, weil sich das Gesicht zu weit bewegt, als dass ein einziger Hochformat-Ausschnitt es halten könnte | {clips_n} içinden {cropped} tanesi dikey kırpıldı; {barred} tanesinde yüz tek bir dikey kadraja sığmayacak kadar hareket ettiği için siyah bant kaldı |  |
| Cut "{quote}" ({round_s_removed}s) | "{quote}" herausgeschnitten ({round_s_removed} s) | "{quote}" kesildi ({round_s_removed} sn) |  |
| Cut "{quote}" ({round_s_removed}s, take {take_index}) | "{quote}" herausgeschnitten ({round_s_removed} s, Take {take_index}) | "{quote}" kesildi ({round_s_removed} sn, {take_index}. çekim) |  |
| Danish | Dänisch |  |  |
| Day Vlog format | Format: Tagesvlog | Format: Günlük vlog |  |
| Day vlog | Tagesvlog | Günlük vlog |  |
| Dictation must be an audio recording | Ein Diktat muss eine Audioaufnahme sein | Dikte bir ses kaydı olmalı |  |
| Do you have an idea, or would you like me to write one? | Hast du eine Idee, oder soll ich eine schreiben? | Aklında bir fikir var mı, yoksa ben mi yazayım? |  |
| Do you have an idea, or would you like me to write one? You can also skip this text. | Hast du eine Idee, oder soll ich eine schreiben? Du kannst diesen Text auch überspringen. | Aklında bir fikir var mı, yoksa ben mi yazayım? İstersen bu yazıyı atlayabilirsin. |  |
| Do you want me to write a voiceover script from your clips? | Soll ich aus deinen Clips ein Voiceover-Skript schreiben? | Kliplerinden bir seslendirme metni yazmamı ister misin? |  |
| Done | Erledigt | Yapıldı |  |
| Done. | Erledigt. | Yapıldı. |  |
| Done:· | Erledigt:· | Yapıldı:· |  |
| Dutch | Niederländisch |  |  |
| Each clip plays in a pause of your talk, at the end of the sentence where you talk about it. If I can't tell which sentence a clip is about it still plays, at my best guess, and I'll say so. A clip you surely never talk about is left out, I'll name it and offer to put it in anyway | Jeder Clip läuft in einer Pause deiner Rede, am Ende des Satzes, in dem du über ihn sprichst. Wenn ich nicht erkennen kann, zu welchem Satz ein Clip gehört, läuft er trotzdem an der Stelle, die ich am wahrscheinlichsten halte, und ich sag dir das. Ein Clip, über den du ganz sicher nie sprichst, wird weggelassen; ich nenne ihn und biete an, ihn trotzdem einzufügen | Her klip konuşmanda, onu anlattığın cümlenin sonunda bir duraklamada oynar. Hangi cümlenin ilgili olduğunu çıkaramazsam yine de en iyi tahminimde oynar ve bunu söylerim. Kesinlikle hakkında konuşmadığın klip dışarıda kalır, adını söyler ve yine de koymayı öneririm |  |
| Each group plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them. | Jede Gruppe läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. | Her {tr_word} tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur. |  |
| Each sport plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them. | Jede Sportart läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. | Her {tr_word} tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur. |  |
| Each {word} plays as one block; blocks start in the order they first appear, and clips inside a block keep the order you filmed them. | Jede Gruppe nach „{word}“ läuft als ein Block; die Blöcke beginnen in der Reihenfolge, in der sie zuerst vorkommen, und die Clips in einem Block behalten die Reihenfolge, in der du sie gefilmt hast. | Her {tr_word} tek bir blok olarak oynar; bloklar ilk göründükleri sırayla başlar, blok içindeki klipler çektiğin sırayı korur. |  |
| Edited {plural_len_entry_targets} | {plural_len_entry_targets} bearbeitet | {plural_len_entry_targets} düzenlendi |  |
| End the edit when your voice ends ({voice_s} seconds) | Edit beenden, wenn deine Stimme endet ({voice_s} Sekunden) | Sesin bittiğinde bitir ({voice_s} saniye) |  |
| Ending on a full-screen colour with your text isn't available on your iPhone yet | Ein Ende mit einem Vollbild in einer Farbe und deinem Text gibt es auf deinem iPhone noch nicht | Videoyu yazılı tam ekran bir renkle bitirmek iPhone'unda henüz kullanılamıyor |  |
| Ending on another of your clips needs a Talking edit with several clips, which isn't available yet | Mit einem anderen deiner Clips zu enden braucht einen Talking-Edit mit mehreren Clips, und den gibt es noch nicht | Başka bir klibinle bitirmek için birden çok klipli konuşmalı düzenleme gerekiyor, o da henüz yok |  |
| Ends right after “{part_unheard}” is said. | Endet direkt nachdem „{part_unheard}“ gesagt wurde. | “{part_unheard}” dendiği yerin hemen ardından biter. |  |
| Engine noise reduction is only available for a single-source Talking edit | Motorgeräusch-Reduzierung gibt es nur für einen Talking-Edit mit einer einzigen Quelle | Motor gürültüsünü azaltma yalnızca tek klipli Konuşmalı düzenlemede yapılabiliyor |  |
| Engine noise reduction isn't available for this edit | Motorgeräusch-Reduzierung ist für diesen Edit nicht verfügbar | Motor gürültüsü azaltma bu düzenlemede kullanılamıyor |  |
| English | Englisch |  |  |
| Every clip gets at least {floor_s:g} seconds, so all {count} stay in. | Jeder Clip bekommt mindestens {floor_s:g} Sekunden, so bleiben alle {count} drin. | Her klip en az {floor_s:g} saniye görünür, böylece {count} klibin hepsi kalır. |  |
| Every clip's own sound plays softly under your voice, not just this one | Der Originalton jedes Clips läuft leise unter deiner Stimme, nicht nur bei diesem | Her klibin kendi sesi seslendirmenin altında kısık çalıyor, yalnızca bu klibinki değil |  |
| Every photo plays the length you gave it, with the footage around it. | Jedes Foto läuft so lange, wie du es festgelegt hast, mit dem Material drumherum. | Her fotoğraf verdiğin süre kadar kalır, çekimler de etrafında oynar. |  |
| Every time | Jedes Mal | Her seferinde |  |
| Every video clip | Jeder Videoclip | Her video klibi |  |
| Every voice in your take gets captions, off-camera ones too | Jede Stimme in deinem Take bekommt Untertitel, auch die außerhalb des Bildes | Çekimindeki her ses altyazıya girer, kamera dışındaki sesler de |  |
| Everything else is unchanged. | Alles andere ist unverändert. | Geri kalan her şey aynı. |  |
| Exclude {font} from automatic {role} text selection. | {font} bei der automatischen Schriftwahl für {role} ausschließen. | {head} için otomatik yazı tipi seçiminde {font} kullanılmasın. |  |
| Extend it to {shown} seconds | Auf {shown} Sekunden verlängern | Süreyi {shown} saniyeye uzat |  |
| Fast pacing | Tempo: Schnell | Tempo: Hızlı |  |
| Favor {font} when automatically choosing {role} fonts. | {font} bei der automatischen Schriftwahl für {role} bevorzugen. | {head} için yazı tipi otomatik seçilirken {font} tercih edilsin. |  |
| Filming hour on {len_timed} of {total} clips; {missing} has no filming time. | Die Aufnahmeuhrzeit steht bei {len_timed} von {total} Clips; {missing} hat keine Aufnahmezeit. | Çekim saati {total} klibin {len_timed} tanesinde var; {missing} klibin çekim saati yok. |  |
| Filming hour on {len_timed} of {total} clips; {missing} have no filming time. | Die Aufnahmeuhrzeit steht bei {len_timed} von {total} Clips; {missing} haben keine Aufnahmezeit. | Çekim saati {total} klibin {len_timed} tanesinde var; {missing} klibin çekim saati yok. |  |
| Fix undone in your draft. | Korrektur in deinem Entwurf rückgängig gemacht. | Düzeltme taslağında geri alındı. |  |
| Fix waiting for your approval. | Korrektur wartet auf deine Freigabe. | Düzeltme onayını bekliyor. |  |
| Fixed and checked again. | Korrigiert und nochmal geprüft. | Düzeltildi ve tekrar kontrol edildi. |  |
| Fixed, but not confirmed. | Korrigiert, aber nicht bestätigt. | Düzeltildi ama doğrulanamadı. |  |
| Font → {font_display_first} on {who} | {who}: Schrift → {font_display_first} | {who} için yazı tipi → {font_display_first} |  |
| For the title, tell me the words or say "continue without a title". | Sag mir für den Titel die Wörter oder schreib "Ohne Titel weiter". | Başlık için kelimeleri söyle ya da "başlık olmasın" yaz. |  |
| For the {genre} song you asked for, I suggest {song_en}. Add it when you post. | Für den gewünschten Song im Stil {genre} schlage ich dir {song_en} vor. Füg ihn beim Posten hinzu. | {song_tr}, istediğin {genre} şarkısı için önerim. Paylaşırken ekleyebilirsin. |  |
| For {names}, the photo shows as a small card in the corner, not full screen | Bei {names} erscheint das Foto als kleine Karte in der Ecke, nicht im Vollbild | {names} için fotoğraf tam ekran değil, köşede küçük bir kart olarak çıkıyor |  |
| For {names}, the photo shows full screen, not as a small card in the corner | Bei {names} erscheint das Foto im Vollbild, nicht als kleine Karte in der Ecke | {names} için fotoğraf köşede küçük bir kart değil, tam ekran çıkıyor |  |
| Freezes the frame for {seconds}s {where} | Hält das Bild {seconds} s an, {where} | Kare {seconds} sn donuyor, {where_tr} |  |
| Freezes the frame for {seconds}s {where}, as you asked later | Hält das Bild {seconds} s an, {where}, wie du später gewünscht hast | Kare {seconds} sn donuyor, {where_tr}, sonra istediğin gibi |  |
| Freezes the frame for {seconds}s {where}, not the {requested:g}s you asked for: a freeze holds at least {seconds}s | Hält das Bild {seconds} s an, {where}, nicht die gewünschten {requested:g} s: ein Standbild dauert mindestens {seconds} s | Kare {seconds} sn donuyor, {where_tr}, istediğin {requested:g} sn değil: bir donma en {v2} {seconds} sn sürebiliyor |  |
| Freezes the frame for {seconds}s {where}, not the {requested:g}s you asked for: a freeze holds at most {seconds}s | Hält das Bild {seconds} s an, {where}, nicht die gewünschten {requested:g} s: ein Standbild dauert höchstens {seconds} s | Kare {seconds} sn donuyor, {where_tr}, istediğin {requested:g} sn değil: bir donma en {v2} {seconds} sn sürebiliyor |  |
| Freezes the frame for {seconds}s {where}; you didn't say how long, so I held it for one second | Hält das Bild {seconds} s an, {where}; du hast nicht gesagt, wie lange, deshalb habe ich eine Sekunde genommen | Kare {seconds} sn donuyor, {where_tr}; süre söylemediğin için bir saniye tuttum |  |
| Freezes the frame for {seconds}s {where}; you didn't say how long, so I made it longer than {previous:g}s | Hält das Bild {seconds} s an, {where}; du hast nicht gesagt, wie lange, deshalb habe ich es länger als {previous:g} s gemacht | Kare {seconds} sn donuyor, {where_tr}; süre söylemediğin için {previous:g} sn'den {v2} tuttum |  |
| Freezes the frame for {seconds}s {where}; you didn't say how long, so I made it shorter than {previous:g}s | Hält das Bild {seconds} s an, {where}; du hast nicht gesagt, wie lange, deshalb habe ich es kürzer als {previous:g} s gemacht | Kare {seconds} sn donuyor, {where_tr}; süre söylemediğin için {previous:g} sn'den {v2} tuttum |  |
| French | Französisch |  |  |
| Full screen isn't available for this edit yet, so "{name}" shows as a small card in the corner instead. | Vollbild gibt es für diesen Edit noch nicht, deshalb erscheint "{name}" stattdessen als kleine Karte in der Ecke. | Bu düzenlemede tam ekran henüz yok; "{name}" köşede küçük bir kart olarak çıkacak. |  |
| Full-screen Visuals aren't available for this edit. | Visuals im Vollbild sind für diesen Edit nicht verfügbar. | Tam ekran Visuals bu düzenleme için yok. |  |
| Full-screen overlays on an already-rendered iPhone edit need a fresh edit — they can't be switched on in place. | Vollbild-Einblendungen in einem schon erstellten iPhone-Edit brauchen einen neuen Edit – sie lassen sich nicht direkt einschalten. | Hazır bir iPhone düzenlemesinde görselleri tam ekran yapmak için yeni bir düzenleme gerekiyor — mevcut düzenlemede açılamıyor. |  |
| German | Deutsch |  |  |
| Got it. What else should we change? | Alles klar. Was sollen wir noch ändern? | Tamam. Başka neyi değiştirelim? |  |
| Greek | Griechisch |  |  |
| Group by group, chronological inside each group | In Gruppen ordnen, innerhalb jeder Gruppe chronologisch | Her {tr_word} bir blok olsun; blok içinde kronolojik sırala |  |
| Group by sport, chronological inside each sport | Nach Sportart gruppieren, innerhalb jeder Sportart chronologisch | Her {tr_word} bir blok olsun; blok içinde kronolojik sırala |  |
| Group by {word}, chronological inside each {word} | Nach „{word}“ gruppieren, chronologisch innerhalb jeder „{word}“-Gruppe | Her {tr_word} bir blok olsun; blok içinde kronolojik sırala |  |
| Have a look | Schau mal nach | Bir göz at |  |
| Have a look at these in the video. | Schau dir diese Stellen im Video an. | Bunlara videoda bir göz at. |  |
| Have a look at these in the video: | Schau dir diese Stellen im Video an: | Bunlara videoda bir göz at: |  |
| Heads-up for this video: | Hinweis zu diesem Video: | Bu video için not: |  |
| Here's a script written from what your clips show. | Hier ist ein Skript, geschrieben nach dem, was deine Clips zeigen. | Kliplerinde görünenlere göre yazılmış bir metin burada. |  |
| Here's the simpler version. | Hier ist die einfachere Version. | İşte daha basit sürüm. |  |
| Here's the updated script, written from what your clips show. | Hier ist das aktualisierte Skript, geschrieben nach dem, was deine Clips zeigen. | Kliplerinde görünenlere göre güncellenmiş metin burada. |  |
| Here’s what changed and what I couldn’t do. | Das hat sich geändert und das habe ich nicht geschafft. | İşte değişenler ve yapamadıklarım. |  |
| Here’s what changed. | Das hat sich geändert. | İşte değişiklikler. |  |
| How long should the video be? | Wie lang soll das Video sein? | Video ne kadar uzun olsun? |  |
| How many seconds at the end of {name} show {what}? I'll end that clip just before them, or tell me to leave the whole clip out. | Wie viele Sekunden am Ende von {name} zeigen {what}? Dann beende ich den Clip kurz davor, oder sag mir, dass ich den ganzen Clip weglassen soll. | {name} klibinin sonundaki kaç saniyede {what} görünüyor? Klibi hemen öncesinde bitiririm; ya da klibin tamamını çıkarmamı söyle. |  |
| How many seconds at the start of {name} show {what}? I'll start that clip right after them, or tell me to leave the whole clip out. | Wie viele Sekunden am Anfang von {name} zeigen {what}? Dann starte ich den Clip direkt danach, oder sag mir, dass ich den ganzen Clip weglassen soll. | {name} klibinin başındaki kaç saniyede {what} görünüyor? Klibi hemen sonrasından başlatırım; ya da klibin tamamını çıkarmamı söyle. |  |
| How short should the clips be? | Wie kurz sollen die Clips sein? | Klipler ne kadar kısa olsun? |  |
| How short should the images be? | Wie kurz sollen die Bilder sein? | Görseller ne kadar kısa olsun? |  |
| How short should the videos be? | Wie kurz sollen die Videos sein? | Videolar ne kadar kısa olsun? |  |
| How short should the {v1} be? | Wie kurz soll {v1} sein? | {v2} ne kadar kısa olsun? |  |
| I accepted the omissions listed on your simpler-version card. | Ich habe die Auslassungen von deiner Karte für die einfachere Version akzeptiert. | Daha basit sürüm kartında belirtilen çıkarmaları onayladım. |  |
| I added it on only {placed} of the {heard} times you said "{trigger}" | Ich habe es nur bei {placed} der {heard} Male hinzugefügt, bei denen du "{trigger}" gesagt hast | "{trigger}" dediğin {heard} yerden yalnızca {placed} tanesine ekleyebildim |  |
| I applied that to the captions you added in chat (free texts, not clip labels). | Das habe ich auf die Texte angewendet, die du im Chat hinzugefügt hast (freie Texte, keine Clip-Labels). | Bunu sohbette eklediğin yazılara uyguladım (klip etiketlerine değil, serbest yazılara). |  |
| I can apply at most {max_intents} clip instructions at a time, so {overflow} more weren't included | Ich kann höchstens {max_intents} Clip-Anweisungen auf einmal umsetzen, deshalb sind {overflow} weitere nicht dabei | Bir seferde en fazla {max_intents} klip talimatı uygulayabiliyorum, bu yüzden {overflow} talimat daha eklenmedi |  |
| I can give the title only one entrance on this video, so it follows another of your asks, not this one | Ich kann dem Titel in diesem Video nur eine Eingangsanimation geben, deshalb folgt er einer anderen deiner Anfragen, nicht dieser | Bu videoda başlığa yalnızca tek bir giriş verebiliyorum, o yüzden bu isteğini değil, başka bir isteğini izliyor |  |
| I can inspect your footage, prepare reversible draft edits, explain what changed, and render only after you approve the exact draft. | Ich kann dein Material ansehen, rückgängig machbare Entwurfsänderungen vorbereiten, erklären, was sich geändert hat, und das Video erst erstellen, wenn du genau diesen Entwurf freigibst. | Çekimlerini inceleyebilir, geri alınabilir taslak düzenlemeler hazırlayabilir, neyin değiştiğini açıklayabilirim. Videoyu yalnızca taslağı onayladıktan sonra oluştururum. |  |
| I can keep a part out only at the start or the end of a clip. Is {what} at the start or the end of {name}, and for how many seconds? Or tell me to leave the whole clip out. | Ich kann einen Teil nur am Anfang oder am Ende eines Clips weglassen. Ist {what} am Anfang oder am Ende von {name}, und wie viele Sekunden lang? Oder sag mir, dass ich den ganzen Clip weglassen soll. | Bir klipten yalnızca başındaki ya da sonundaki bir bölümü çıkarabiliyorum. {what} {name} klibinin başında mı sonunda mı, kaç saniye sürüyor? Ya da klibin tamamını çıkarmamı söyle. |  |
| I can keep up to {max_creator_pinned_texts} lines of corner text. Which ones should stay? | Ich kann bis zu {max_creator_pinned_texts} Zeilen Text in der Ecke behalten. Welche sollen bleiben? | Köşede en fazla {max_creator_pinned_texts} satır yazı tutabilirim. Hangileri kalsın? |  |
| I can keep your words exactly as written on that clip, or use the shorter label "{shorter}". | Ich kann deine Wörter genau so auf dem Clip lassen, wie du sie geschrieben hast, oder das kürzere Label "{shorter}" nehmen. | Yazdığın gibi aynen o klipte gösterebilirim ya da daha kısa "{shorter}" etiketini kullanabilirim. |  |
| I can keep your words exactly as written on that clip, or you can give me a shorter label. | Ich kann deine Wörter genau so auf dem Clip lassen, wie du sie geschrieben hast, oder du gibst mir ein kürzeres Label. | Yazdığın gibi aynen o klipte gösterebilirim ya da bana daha kısa bir etiket yazabilirsin. |  |
| I can only label clips from facts I have (their place, landmark or filming time), so I can't tell which part of the day each clip shows. Tell me what each clip is (for example 'clip 1 is the airport pickup, clip 2 is the pre-wedding') and I'll write those on the clips, or I can label them with the place names or the times instead. | Ich kann Clips nur mit Angaben beschriften, die ich habe (Ort, Wahrzeichen oder Aufnahmezeit), deshalb weiß ich nicht, welchen Teil des Tages jeder Clip zeigt. Sag mir, was jeder Clip ist (zum Beispiel 'Clip 1 ist die Abholung am Flughafen, Clip 2 ist vor der Hochzeit'), dann schreibe ich das auf die Clips, oder ich beschrifte sie stattdessen mit den Ortsnamen oder den Uhrzeiten. | Klipleri yalnızca elimdeki bilgilerle (yer, mekân ya da çekim saati) etiketleyebilirim, o yüzden her klibin günün hangi kısmını gösterdiğini bilemem. Her klibin ne olduğunu söyle (örneğin 'klip 1 havalimanı karşılaması, klip 2 düğün öncesi'), ben de bunları kliplerin üzerine yazayım; istersen yer adlarıyla ya da saatlerle de etiketleyebilirim. |  |
| I can only put a photo or video from this video's Visuals behind a clip, and I couldn't find that one there. | Ich kann nur ein Foto oder Video aus den Visuals dieses Videos hinter einen Clip legen, und das hier habe ich dort nicht gefunden. | Bir klibin arkasına yalnızca bu videonun görsellerinden bir fotoğraf ya da video koyabilirim ve onu orada bulamadım. |  |
| I can rebuild the edit from your confirmed plan, or you can ask for a different edit. | Ich kann den Edit nach deinem bestätigten Plan neu bauen, oder du wünschst dir einen anderen Edit. | Düzenlemeyi onayladığın plana göre yeniden kurabilirim ya da farklı bir düzenleme isteyebilirsin. |  |
| I can rebuild the edit so it shows this, or you can relax the requirement. | Ich kann den Edit so neu bauen, dass er das zeigt, oder du lockerst die Vorgabe. | Düzenlemeyi bunu gösterecek şekilde yeniden kurabilirim ya da bu isteği esnetebilirsin. |  |
| I can rebuild the edit with that soundtrack playing once, or you can pick a different one. | Ich kann den Edit so neu bauen, dass dieser Ton einmal läuft, oder du wählst einen anderen. | Düzenlemeyi o ses bir kez çalacak şekilde yeniden kurabilirim ya da başka bir ses seçebilirsin. |  |
| I can rebuild the edit without that soundtrack, or you can ask for music. | Ich kann den Edit ohne diesen Ton neu bauen, oder du wünschst dir Musik. | Düzenlemeyi o ses olmadan yeniden kurabilirim ya da müzik isteyebilirsin. |  |
| I can render it again, or you can relax the requirement. | Ich kann das Video nochmal erstellen, oder du lockerst die Vorgabe. | Videoyu yeniden oluşturabilirim ya da bu isteği esnetebilirsin. |  |
| I can use the shorter label "{shorter}" instead, or you can write another one. | Ich kann stattdessen das kürzere Label "{shorter}" nehmen, oder du schreibst ein anderes. | Onun yerine daha kısa "{shorter}" etiketini kullanabilirim ya da başka bir tane yazabilirsin. |  |
| I can't add a title without them. | Ohne sie kann ich keinen Titel hinzufügen. | Kelimeler olmadan başlık ekleyemem. |  |
| I can't add music under the spoken lines on this video yet, so it plays the clips' own sound | Ich kann in diesem Video noch keine Musik unter die gesprochenen Zeilen legen, deshalb läuft der Originalton der Clips | Bu videoda konuşmaların altına henüz müzik ekleyemiyorum, o yüzden kliplerin kendi sesi çalacak |  |
| I can't add sound effects on this video yet, so none will be played | Ich kann in diesem Video noch keine Soundeffekte hinzufügen, deshalb wird keiner abgespielt | Bu videoya henüz ses efekti ekleyemiyorum, hiçbiri çalmayacak |  |
| I can't add sound effects to an edit cut from what you say yet | Ich kann noch keine Soundeffekte zu einem Edit hinzufügen, der nach dem Gesagten geschnitten ist | Söylediklerinden kesilen bir düzenlemeye henüz ses efekti ekleyemiyorum |  |
| I can't add subtitles of the speech to this video yet, so it has none | Ich kann diesem Video noch keine Untertitel der Sprache hinzufügen, deshalb hat es keine | Bu videoya henüz konuşmanın altyazısını ekleyemiyorum, o yüzden altyazı yok |  |
| I can't add the closing title ("{clip_value}") on this video yet | Ich kann den Schlusstitel ("{clip_value}") in diesem Video noch nicht hinzufügen | Kapanış başlığını ("{clip_value}") bu videoya henüz ekleyemiyorum |  |
| I can't add the opening title ("{clip_value}") on this video yet | Ich kann den Anfangstitel ("{clip_value}") in diesem Video noch nicht hinzufügen | Açılış başlığını ("{clip_value}") bu videoya henüz ekleyemiyorum |  |
| I can't add the treatments ({list_items}) on this video yet, so they won't be applied | Ich kann die Effekte ({list_items}) in diesem Video noch nicht hinzufügen, deshalb werden sie nicht angewendet | Şu efektleri ({list_items}) bu videoda henüz ekleyemiyorum, uygulanmayacak |  |
| I can't add the treatments ({listed_items}) on this video yet, so they won't be applied | Ich kann die Effekte ({listed_items}) in diesem Video noch nicht hinzufügen, deshalb werden sie nicht angewendet | Şu efektleri ({listed_items}) bu videoda henüz ekleyemiyorum, uygulanmayacak |  |
| I can't aim or zoom the recordings behind you on this edit. | Ich kann die Aufnahmen hinter dir in diesem Edit nicht ausrichten oder zoomen. | Arkandaki kayıtları bu düzenlemede hizalayamıyor ya da yakınlaştıramıyorum. |  |
| I can't animate on-screen text on this video yet, so the text shows without motion | Ich kann Text auf dem Bildschirm in diesem Video noch nicht animieren, deshalb erscheint er ohne Bewegung | Bu videoda ekrandaki yazıyı henüz canlandıramıyorum, o yüzden yazı hareketsiz görünecek |  |
| I can't animate the title ({words_value}) on this video yet, so it shows without motion | Ich kann den Titel ({words_value}) in diesem Video noch nicht animieren, deshalb erscheint er ohne Bewegung | Başlığı ({words_value}) bu videoda henüz canlandıramıyorum, o yüzden hareketsiz görünecek |  |
| I can't apply the planned text font and color on this video yet | Ich kann die geplante Schrift und Farbe in diesem Video noch nicht anwenden | Planlanan yazı tipini ve rengini bu videoda henüz uygulayamıyorum |  |
| I can't apply the {value} caption style on this video yet, so it won't be used | Ich kann den Untertitel-Stil {value} in diesem Video noch nicht anwenden, deshalb wird er nicht verwendet | {value} altyazı stilini bu videoda henüz uygulayamıyorum, kullanılmayacak |  |
| I can't apply this whole correction with the saved draft's supported editor tools. Nothing was changed. Ask for a single text or style change, or ask for a new version using your saved request. | Ich kann diese ganze Korrektur nicht mit den Editor-Werkzeugen des gespeicherten Entwurfs umsetzen. Es wurde nichts geändert. Bitte um eine einzelne Text- oder Stiländerung oder um eine neue Version aus deiner gespeicherten Anfrage. | Bu düzeltmenin tamamını kayıtlı taslağın desteklediği düzenleme araçlarıyla uygulayamıyorum. Hiçbir şey değişmedi. Tek bir yazı veya stil değişikliği iste ya da kayıtlı isteğinden yeni bir sürüm iste. |  |
| I can't change clip speed on this edit because the clips follow your song, so they play at normal speed | Ich kann in diesem Edit das Clip-Tempo nicht ändern, weil die Clips deinem Song folgen, deshalb laufen sie im normalen Tempo | Bu düzenlemede klip hızını değiştiremiyorum çünkü klipler şarkını takip ediyor, o yüzden normal hızda oynuyorlar |  |
| I can't change clip speed on your iPhone yet, so the clips play at normal speed | Ich kann das Clip-Tempo auf deinem iPhone noch nicht ändern, deshalb laufen die Clips im normalen Tempo | iPhone'unda klip hızını henüz değiştiremiyorum, o yüzden klipler normal hızda oynuyor |  |
| I can't change how bright or dark clips look in this edit yet, so they keep their own light | Ich kann in diesem Edit noch nicht ändern, wie hell oder dunkel Clips aussehen, deshalb behalten sie ihr eigenes Licht | Bu düzenlemede kliplerin ne kadar aydınlık ya da karanlık göründüğünü henüz değiştiremiyorum, o yüzden kendi ışıklarıyla kalıyorlar |  |
| I can't change that on this edit yet. | Das kann ich in diesem Edit noch nicht ändern. | Bunu bu düzenlemede henüz değiştiremiyorum. |  |
| I can't change the speed of a clip that holds a freeze frame, so it plays at normal speed | Ich kann das Tempo eines Clips mit Standbild nicht ändern, deshalb läuft er im normalen Tempo | Donmuş kare tutan bir klibin hızını değiştiremiyorum, o yüzden normal hızda oynuyor |  |
| I can't check that order against your clips, so I can't promise it. | Ich kann diese Reihenfolge nicht mit deinen Clips abgleichen, deshalb kann ich sie nicht versprechen. | Bu sırayı kliplerinle karşılaştırıp doğrulayamıyorum, bu yüzden söz veremem. |  |
| I can't check the captions on this draft yet. | Ich kann die Untertitel in diesem Entwurf noch nicht prüfen. | Bu taslaktaki altyazıları henüz kontrol edemiyorum. |  |
| I can't check the pop-ins on this draft yet. | Ich kann die Einblendungen in diesem Entwurf noch nicht prüfen. | Bu taslakta sözlerine göre çıkan görselleri henüz kontrol edemiyorum. |  |
| I can't check the small card on this draft yet. | Ich kann die kleine Karte in diesem Entwurf noch nicht prüfen. | Bu taslakta küçük kartı henüz kontrol edemiyorum. |  |
| I can't check the sound mix on this draft yet. | Ich kann den Tonmix in diesem Entwurf noch nicht prüfen. | Bu taslakta ses karışımını henüz kontrol edemiyorum. |  |
| I can't check the speech cleanup on this draft yet. | Ich kann die Sprachbereinigung in diesem Entwurf noch nicht prüfen. | Bu taslakta konuşma temizliğini henüz kontrol edemiyorum. |  |
| I can't check the spoken parts on this draft yet. | Ich kann die gesprochenen Teile in diesem Entwurf noch nicht prüfen. | Bu taslaktaki konuşma bölümlerini henüz kontrol edemiyorum. |  |
| I can't check this before the video is made; I'll point it out after. | Das kann ich nicht prüfen, bevor das Video erstellt ist; ich weise danach darauf hin. | Bunu video hazırlanmadan önce kontrol edemiyorum; sonra sana göstereceğim. |  |
| I can't check this text style automatically. | Ich kann diesen Textstil nicht automatisch prüfen. | Bu yazı stilini otomatik olarak kontrol edemiyorum. |  |
| I can't check which clips you wanted left out | Ich kann nicht prüfen, welche Clips du weglassen wolltest | Hangi klipleri dışarıda bırakmak istediğini kontrol edemiyorum |  |
| I can't confirm the order this draft uses. | Ich kann die Reihenfolge, die dieser Entwurf nutzt, nicht bestätigen. | Bu taslağın kullandığı sırayı doğrulayamıyorum. |  |
| I can't confirm this draft does it yet. | Ich kann noch nicht bestätigen, dass dieser Entwurf das macht. | Bu taslağın bunu yaptığını henüz doğrulayamıyorum. |  |
| I can't confirm this draft keeps your whole take. | Ich kann nicht bestätigen, dass dieser Entwurf deinen ganzen Take behält. | Bu taslağın çekimini baştan sona koruduğunu doğrulayamıyorum. |  |
| I can't confirm this draft's length yet. | Ich kann die Länge dieses Entwurfs noch nicht bestätigen. | Bu taslağın uzunluğunu henüz doğrulayamıyorum. |  |
| I can't confirm where this draft's title came from. | Ich kann nicht bestätigen, woher der Titel dieses Entwurfs kommt. | Bu taslaktaki başlığın nereden geldiğini doğrulayamıyorum. |  |
| I can't confirm {text} still has the light fill and dark outline I chose for the footage: {why} | Ich kann nicht bestätigen, dass {text} noch die helle Füllung und die dunkle Kontur hat, die ich für das Material gewählt habe: {why} | {text} yazısında görüntü için seçtiğim açık dolgu ve koyu kenar çizgisi duruyor mu, doğrulayamıyorum: {why} |  |
| I can't crossfade between clips on this video yet, so each clip cuts straight to the next | Ich kann in diesem Video noch nicht zwischen Clips überblenden, deshalb springt jeder Clip direkt zum nächsten | Bu videoda klipler arasında henüz yumuşak geçiş yapamıyorum, o yüzden her klip bir sonrakine doğrudan kesiliyor |  |
| I can't curve text in this edit on your iPhone yet, so it stays straight | Ich kann Text in diesem Edit auf deinem iPhone noch nicht biegen, deshalb bleibt er gerade | Bu düzenlemede iPhone'unda yazıyı henüz kavisli yapamıyorum, o yüzden düz kalıyor |  |
| I can't cut a line out of a video made of several takes yet | aus einem Video aus mehreren Takes kann ich noch keinen Satz herausschneiden | birkaç çekimden oluşan bir videodan henüz satır kesemiyorum |  |
| I can't cut anything out of a video made of several takes yet, so the pauses and lines you asked me to cut stay in | Ich kann aus einem Video aus mehreren Takes noch nichts herausschneiden, deshalb bleiben die Pausen und Zeilen, die ich schneiden sollte, drin | Birkaç çekimden oluşan bir videodan henüz bir şey kesemiyorum, o yüzden kesmemi istediğin duraklamalar ve satırlar kalıyor |  |
| I can't cut one exact line out of a video made of several takes yet, so it stays in | Ich kann aus einem Video aus mehreren Takes noch keine einzelne Zeile herausschneiden, deshalb bleibt sie drin | Birkaç çekimden oluşan bir videodan henüz tek bir satırı kesemiyorum, o yüzden kalıyor |  |
| I can't cut out one exact line yet or confirm it's gone, so it may still be in your video | Ich kann eine bestimmte Zeile noch nicht genau herausschneiden oder bestätigen, dass sie weg ist, deshalb ist sie vielleicht noch in deinem Video | belirli bir cümleyi henüz tam olarak kesemiyorum ve çıkarıldığını doğrulayamıyorum, o yüzden videoda kalmış olabilir |  |
| I can't cut the end of a video made of several takes yet, so it runs to the end of your last take | Ich kann das Ende eines Videos aus mehreren Takes noch nicht kürzen, deshalb läuft es bis zum Ende deines letzten Takes | Birkaç çekimden oluşan bir videonun sonunu henüz kesemiyorum, o yüzden son çekiminin sonuna kadar sürüyor |  |
| I can't cut the start of a video made of several takes yet, so it opens at the beginning of your first take | Ich kann den Anfang eines Videos aus mehreren Takes noch nicht kürzen, deshalb beginnt es am Anfang deines ersten Takes | Birkaç çekimden oluşan bir videonun başını henüz kesemiyorum, o yüzden ilk çekiminin başından açılıyor |  |
| I can't do that on this edit: {str_exc_strip_rstrip}. Nothing was changed. | Das geht in diesem Edit nicht: {str_exc_strip_rstrip}. Es wurde nichts geändert. | Bu düzenlemede bunu yapamıyorum: {str_exc_strip_rstrip}. Hiçbir şey değişmedi. |  |
| I can't do {named} with your clips yet, so this draft leaves it out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I'll add it. | {named} kann ich mit deinen Clips noch nicht machen, deshalb lässt dieser Entwurf es weg. Ich kann Clips verwenden, überspringen, umsortieren, gruppieren, beschriften oder mit Untertiteln versehen: Sag mir, was davon du meintest, dann füge ich es hinzu. | {named} isteğini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim. |  |
| I can't do {named} with your clips yet, so this draft leaves them out. I can use, skip, reorder, group, label or caption clips: tell me which of those you meant and I'll add it. | {named} kann ich mit deinen Clips noch nicht machen, deshalb lässt dieser Entwurf sie weg. Ich kann Clips verwenden, überspringen, umsortieren, gruppieren, beschriften oder mit Untertiteln versehen: Sag mir, was davon du meintest, dann füge ich es hinzu. | {named} isteklerini kliplerinle henüz yapamıyorum, bu yüzden bu taslakta yok. Klipleri kullanabilir, atlayabilir, sıralayabilir, gruplayabilir, etiketleyebilir ya da üzerlerine yazı ekleyebilirim: hangisini kastettiğini söylersen eklerim. |  |
| I can't draw a line around someone in the video yet, so it won't have one | Ich kann noch keine Linie um jemanden im Video zeichnen, deshalb gibt es keine | Videodaki birinin etrafına henüz çizgi çizemiyorum, o yüzden videoda olmayacak |  |
| I can't feature one hero clip on this video yet, so every clip is cut the same way | Ich kann in diesem Video noch keinen Haupt-Clip hervorheben, deshalb werden alle Clips gleich geschnitten | Bu videoda tek bir ana klibi öne çıkaramıyorum, bu yüzden bütün klipler aynı şekilde kesiliyor |  |
| I can't fix label timing that way: that tool only changes label wording. Ask me to realign the labels to their clips and I'll line them up. | So kann ich das Timing der Labels nicht korrigieren: dieses Werkzeug ändert nur den Text der Labels. Bitte mich, die Labels neu an ihren Clips auszurichten, dann richte ich sie aus. | Etiketlerin zamanlamasını bu şekilde düzeltemem: bu araç yalnızca etiketlerin yazısını değiştiriyor. Etiketleri kliplerine göre yeniden hizalamamı iste, ben de hizalayayım. |  |
| I can't follow the planned story structure ({list_items}) on this video yet, so it won't change the order of the clips | Ich kann die geplante Story-Struktur ({list_items}) in diesem Video noch nicht umsetzen, deshalb ändert sie die Reihenfolge der Clips nicht | Planlanan hikâye yapısını ({list_items}) bu videoda henüz uygulayamıyorum, o yüzden kliplerin sırasını değiştirmeyecek |  |
| I can't follow the planned story structure ({listed_items}) on this video yet, so it won't change the order of the clips | Ich kann die geplante Story-Struktur ({listed_items}) in diesem Video noch nicht umsetzen, deshalb ändert sie die Reihenfolge der Clips nicht | Planlanan hikâye yapısını ({listed_items}) bu videoda henüz uygulayamıyorum, o yüzden kliplerin sırasını değiştirmeyecek |  |
| I can't freeze a frame in this edit yet, so that moment plays normally | Ich kann in diesem Edit noch kein Standbild setzen, deshalb läuft dieser Moment normal | Bu düzenlemede henüz bir kareyi donduramıyorum, o yüzden o an normal akıyor |  |
| I can't hear the speech in that clip yet | Ich kann die Sprache in dem Clip noch nicht hören | O klipteki konuşmayı henüz duyamıyorum |  |
| I can't keep that exact alternating cut with a voiceover. Should I drop the voiceover, or change the cut? | Ich kann diesen genauen Wechselschnitt mit einem Voiceover nicht beibehalten. Soll ich das Voiceover weglassen oder den Schnitt ändern? | Bu tam dönüşümlü kesimi seslendirmeyle birlikte koruyamıyorum. Seslendirmeyi çıkarayım mı, yoksa kesimi mi değiştireyim? |  |
| I can't keep that exact photo and video timing in a voiceover edit. | Ich kann dieses genaue Foto- und Video-Timing in einem Voiceover-Edit nicht beibehalten. | Seslendirmeli düzenlemede o tam fotoğraf ve video zamanlamasını koruyamıyorum. |  |
| I can't keep that exact photo and video timing in this edit. Should I make it without the exact timing? | Ich kann dieses genaue Foto- und Video-Timing in diesem Edit nicht beibehalten. Soll ich es ohne das genaue Timing machen? | Bu düzenlemede o tam fotoğraf ve video zamanlamasını koruyamıyorum. Tam zamanlama olmadan yapayım mı? |  |
| I can't keep the exact alternating cut you asked for on this edit, so it is cut as a regular montage | Ich kann den gewünschten Wechselschnitt in diesem Edit nicht genau beibehalten, deshalb wird er wie eine normale Montage geschnitten | Bu düzenlemede istediğin dönüşümlü kesimi aynen koruyamıyorum, o yüzden normal bir montaj olarak kesiliyor |  |
| I can't keep the photo and video timing you asked for on this edit, so it uses the standard timing | Ich kann das gewünschte Foto- und Video-Timing in diesem Edit nicht beibehalten, deshalb nutzt er das Standard-Timing | Bu düzenlemede istediğin fotoğraf ve video zamanlamasını koruyamıyorum, o yüzden standart zamanlama kullanılıyor |  |
| I can't make this length without showing the part of a clip you asked me to leave out. Ask for a different length, or leave that clip out. | Ich kann diese Länge nicht erreichen, ohne den Teil eines Clips zu zeigen, den ich weglassen sollte. Wünsch dir eine andere Länge oder lass diesen Clip weg. | Gösterme dediğin klip bölümünü göstermeden bu uzunluğu yapamıyorum. Başka bir uzunluk iste ya da o klibi dışarıda bırak. |  |
| I can't make this video the length you asked for yet: {instead_en} | Ich kann dieses Video noch nicht in der gewünschten Länge machen: {instead_en} | Bu videoyu henüz istediğin uzunlukta yapamıyorum: {instead_tr} |  |
| I can't make this yet: | Das kann ich noch nicht: | Bunu henüz yapamıyorum: |  |
| I can't make {language} captions yet, so they'll be in the language that's spoken | Ich kann noch keine Untertitel auf {language} erstellen, deshalb sind sie in der gesprochenen Sprache | Altyazıları şimdilik {language} yapamıyorum, konuşulan dilde olacaklar |  |
| I can't move the title to the spot you asked for on this video yet, so it stays where the layout puts it | Ich kann den Titel in diesem Video noch nicht an die gewünschte Stelle verschieben, deshalb bleibt er dort, wo das Layout ihn hinsetzt | Bu videoda başlığı henüz istediğin yere taşıyamıyorum, o yüzden düzenin koyduğu yerde kalıyor |  |
| I can't place the clip labels "{clip_value}" yet, so they stay where the layout puts them | Ich kann die Clip-Labels "{clip_value}" noch nicht platzieren, deshalb bleiben sie dort, wo das Layout sie hinsetzt | Klip etiketlerini henüz "{clip_value}" konumuna yerleştiremiyorum, o yüzden düzenin koyduğu yerde kalıyorlar |  |
| I can't place the clip labels at the {words_value} on this video yet | Ich kann die Clip-Labels in diesem Video noch nicht bei {words_value} platzieren | Klip etiketlerini bu videoda henüz {words_value} konumuna yerleştiremiyorum |  |
| I can't play that title entrance on this phone yet, so the title fades in instead | Ich kann diese Eingangsanimation des Titels auf diesem Handy noch nicht abspielen, deshalb blendet der Titel stattdessen sanft ein | Bu başlık girişini bu telefonda henüz oynatamıyorum, o yüzden başlık onun yerine yavaşça beliriyor |  |
| I can't put a photo or video behind a clip on this edit. | Ich kann in diesem Edit kein Foto oder Video hinter einen Clip legen. | Bu videoda bir klibin arkasına fotoğraf ya da video koyamıyorum. |  |
| I can't put a plain colour behind you: {reason}. | Ich kann keine einfarbige Fläche hinter dich legen: {reason}. | Arkana düz bir renk koyamıyorum: bu uygulama sürümünde henüz açık değil. |  |
| I can't put text on each shot and keep exact photo and video timing in the same edit. Which one matters more? | Ich kann nicht im selben Edit Text auf jede Einstellung setzen und das genaue Foto- und Video-Timing behalten. Was ist dir wichtiger? | Aynı düzenlemede hem her çekime yazı koyup hem de fotoğraf ve videoların zamanlamasını tam tutamıyorum. Hangisi daha önemli? |  |
| I can't put the planned label on each shot on this video yet | Ich kann in diesem Video noch nicht das geplante Label auf jede Einstellung setzen | Planlanan etiketleri bu videoda henüz her çekime koyamıyorum |  |
| I can't put your Visuals into this montage yet, so I didn't start the render. Remove them from Visuals, then tap Refresh project and I'll make it from your videos. | Ich kann deine Visuals noch nicht in diese Montage einbauen, deshalb habe ich das Erstellen nicht gestartet. Entferne sie aus Visuals, tipp dann auf „Refresh project“, und ich erstelle es aus deinen Videos. | Visuals bölümündeki görsellerini henüz bu montaja ekleyemiyorum, o yüzden videoyu başlatmadım. Onları Visuals bölümünden çıkar, sonra "Refresh project" düğmesine dokun; videoyu senin videolarından hazırlayayım. |  |
| I can't ramp speed inside a clip yet, so it plays at normal speed | Ich kann das Tempo innerhalb eines Clips noch nicht gleitend ändern, deshalb läuft er im normalen Tempo | Bir klibin içinde hızı henüz kademeli değiştiremiyorum, o yüzden normal hızda oynuyor |  |
| I can't repeat or loop your footage on this edit, so no moment of it plays twice | Ich kann dein Material in diesem Edit nicht wiederholen oder loopen, deshalb läuft kein Moment doppelt | Bu düzenlemede çekimlerini tekrarlayamıyor ya da döngüye alamıyorum, o yüzden hiçbir an iki kez oynamıyor |  |
| I can't speed these clips up: the video already shows each one whole, so there's no footage left to speed through | Ich kann diese Clips nicht beschleunigen: das Video zeigt jeden schon komplett, es gibt kein Material mehr, das man schneller durchlaufen lassen könnte | Bu klipleri hızlandıramıyorum: video her birini zaten baştan sona gösteriyor, hızlanacak görüntü kalmadı |  |
| I can't tell exactly where some of your clips sit in the song, and verses repeat. Which part of the song did you film? Pick it below, or use the whole song. | Ich kann nicht genau sagen, wo einige deiner Clips im Song liegen, und Strophen wiederholen sich. Zu welchem Teil des Songs hast du gefilmt? Wähl ihn unten aus oder nimm den ganzen Song. | Bazı kliplerin şarkının neresine denk geldiğini net çıkaramıyorum, üstelik bölümler tekrar ediyor. Şarkının hangi kısmında çektin? Aşağıdan seç ya da tüm şarkıyı kullan. |  |
| I can't tell what those clips show, so I didn't write captions for them. Tell me what each clip is (for example 'clip 1 is the airport pickup') and I'll add them. | Ich kann nicht erkennen, was diese Clips zeigen, deshalb habe ich keine Untertitel dafür geschrieben. Sag mir, was jeder Clip ist (zum Beispiel 'Clip 1 ist die Abholung am Flughafen'), dann füge ich sie hinzu. | Bu kliplerin ne gösterdiğini anlayamadım, o yüzden onlara yazı yazmadım. Her klibin ne olduğunu söyle (örneğin 'klip 1 havalimanı karşılaması'), ben de ekleyeyim. |  |
| I can't tell which clips these moments came from | Ich kann nicht sagen, aus welchen Clips diese Momente stammen | Bu anların hangi kliplerden geldiğini söyleyemiyorum |  |
| I can't tell which of your clips are the long takes | Ich kann nicht sagen, welche deiner Clips die langen Takes sind | Hangi kliplerin uzun çekim olduğunu söyleyemiyorum |  |
| I can't tell who's speaking, so it shows the first time anyone says that word | Ich kann nicht erkennen, wer spricht, deshalb erscheint es, wenn das Wort zum ersten Mal jemand sagt | Kimin konuştuğunu ayırt edemiyorum, o yüzden yazı o kelime ilk söylendiğinde çıkıyor |  |
| I can't tell yet which clip is the talking one | ich weiß noch nicht, in welchem Clip du sprichst | hangi klibin konuşma klibi olduğunu henüz bilmiyorum |  |
| I can't tilt a picture on this edit yet. | Ich kann in diesem Edit noch kein Bild neigen. | Bu videoda bir görseli henüz eğemiyorum. |  |
| I can't tilt clips on this edit. | Ich kann in diesem Edit keine Clips neigen. | Bu videoda klipleri eğemiyorum. |  |
| I can't turn captions off on this kind of edit yet, so the video will have captions | Ich kann bei dieser Art von Edit die Untertitel noch nicht ausschalten, deshalb hat das Video Untertitel | Bu tür bir düzenlemede altyazıları henüz kapatamıyorum, o yüzden videoda altyazı olacak |  |
| I can't turn off just part of a clip on your iPhone yet, so a clip that is only partly in that stretch keeps its sound | Ich kann auf deinem iPhone noch nicht nur einen Teil eines Clips stummschalten, deshalb behält ein Clip, der nur teilweise in diesem Abschnitt liegt, seinen Ton | iPhone'unda bir klibin sadece bir kısmının sesini henüz kapatamıyorum, o yüzden o aralıkta kısmen kalan klip sesini koruyor |  |
| I can't undo that from chat: the edit changed after my last change, or that change can't be put back exactly. Nothing was changed. Use Undo in the editor, or tell me what to change back. | Das kann ich im Chat nicht rückgängig machen: Der Edit hat sich nach meiner letzten Änderung geändert, oder diese Änderung lässt sich nicht exakt zurücksetzen. Es wurde nichts geändert. Nutz „Rückgängig“ im Editor oder sag mir, was ich zurücksetzen soll. | Bunu sohbetten geri alamıyorum: son değişikliğimden sonra düzenleme değişti ya da o değişiklik tam olarak geri getirilemiyor. Hiçbir şey değişmedi. Editördeki Geri Al'ı kullan ya da neyi eski haline getireyim, söyle. |  |
| I can't use the opening hook idea ("{clip_value}") on this video yet, so it won't be shown as text | Ich kann die Hook-Idee für den Anfang ("{clip_value}") in diesem Video noch nicht nutzen, deshalb wird sie nicht als Text angezeigt | Açılış kanca fikrini ("{clip_value}") bu videoda henüz kullanamıyorum, o yüzden yazı olarak gösterilmeyecek |  |
| I can't use the planned overlay display on this video yet, so it won't be shown | Ich kann die geplante Einblendung in diesem Video noch nicht nutzen, deshalb wird sie nicht angezeigt | Planlanan katman gösterimini bu videoda henüz kullanamıyorum, gösterilmeyecek |  |
| I can't use the {str_value_replace} story archetype on this video yet | Das Story-Muster „{str_value_replace}“ kann ich bei diesem Video noch nicht nutzen | {str_value_replace} hikâye kalıbını bu videoda henüz kullanamıyorum |  |
| I can't use the {words_value} story archetype on this video yet | Das Story-Muster „{words_value}“ kann ich bei diesem Video noch nicht nutzen | {words_value} hikâye kalıbını bu videoda henüz kullanamıyorum |  |
| I can't verify per-clip text on an editor edit. | Ich kann Text pro Clip in einem Editor-Edit nicht prüfen. | Editör düzenlemesinde klip başına yazıyı doğrulayamıyorum. |  |
| I can't verify text for that target clip in this draft. | Ich kann den Text für diesen Clip in diesem Entwurf nicht prüfen. | Bu taslakta o klibin yazısını doğrulayamıyorum. |  |
| I can't verify this one automatically yet. | Das kann ich noch nicht automatisch prüfen. | Bunu henüz otomatik olarak doğrulayamıyorum. |  |
| I can't verify this ordering automatically. | Diese Reihenfolge kann ich nicht automatisch prüfen. | Bu sıralamayı otomatik olarak doğrulayamıyorum. |  |
| I can't verify this ordering rule from the approved media. | Ich kann diese Reihenfolge-Regel anhand des freigegebenen Materials nicht prüfen. | Bu sıralama kuralını onaylanan çekimlerden doğrulayamıyorum. |  |
| I can't verify this ordering rule, and the clips are not in an order I can show matches it. | Ich kann diese Reihenfolge-Regel nicht prüfen, und die Clips liegen nicht in einer Reihenfolge, bei der ich zeigen kann, dass sie passt. | Bu sıralama kuralını doğrulayamıyorum ve klipler kurala uyduğunu gösterebileceğim bir sırada değil. |  |
| I can't verify this timing automatically. | Dieses Timing kann ich nicht automatisch prüfen. | Bu süreyi otomatik olarak doğrulayamıyorum. |  |
| I can't zoom clips on this edit. | Ich kann in diesem Edit keine Clips zoomen. | Bu videoda klipleri yakınlaştıramıyorum. |  |
| I cancelled this approval because the draft, footage or available rendering features changed. Your saved edit is unchanged. Ask me to prepare it again from your saved request. | Ich habe diese Freigabe abgebrochen, weil sich der Entwurf, das Material oder die verfügbaren Funktionen geändert haben. Dein gespeicherter Edit ist unverändert. Bitte mich, ihn aus deiner gespeicherten Anfrage neu vorzubereiten. | Taslak, çekimler veya kullanılabilir oluşturma özellikleri değiştiği için bu onayı iptal ettim. Kayıtlı düzenlemen değişmedi. Kayıtlı isteğinden yeniden hazırlamamı iste. |  |
| I changed:· | Das habe ich geändert:· | Değiştirdiklerim:· |  |
| I checked frames from the finished video against your request. Everything I could see matches. I can't judge sound or motion from still frames. | Ich habe Bilder aus dem fertigen Video mit deiner Anfrage abgeglichen. Alles, was ich sehen konnte, passt. Ton und Bewegung kann ich an Standbildern nicht beurteilen. | Bitmiş videodan kareleri isteğinle karşılaştırdım. Görebildiğim her şey uyuyor. Sesi ve hareketi durağan karelerden değerlendiremem. |  |
| I checked the finished video against your request. A few things didn't match, and fixes are waiting for your approval. | Ich habe das fertige Video mit deiner Anfrage abgeglichen. Ein paar Dinge passten nicht, und Korrekturen warten auf deine Freigabe. | Bitmiş videoyu isteğinle karşılaştırdım. Birkaç şey uyuşmadı ve düzeltmeler onayını bekliyor. |  |
| I checked the finished video against your request. Everything I could see matches. | Ich habe das fertige Video mit deiner Anfrage abgeglichen. Alles, was ich sehen konnte, passt. | Bitmiş videoyu isteğinle karşılaştırdım. Görebildiğim her şey uyuyor. |  |
| I checked the finished video against your request. One thing didn't match, and a fix is waiting for your approval. | Ich habe das fertige Video mit deiner Anfrage abgeglichen. Eine Sache passte nicht, und eine Korrektur wartet auf deine Freigabe. | Bitmiş videoyu isteğinle karşılaştırdım. Bir şey uyuşmadı ve bir düzeltme onayını bekliyor. |  |
| I checked the finished video. Something may not match your request. I didn't change anything. | Ich habe das fertige Video geprüft. Etwas passt vielleicht nicht zu deiner Anfrage. Ich habe nichts geändert. | Bitmiş videoyu kontrol ettim. Bir şey isteğinle uyuşmuyor olabilir. Hiçbir şeyi değiştirmedim. |  |
| I checked the new video: the fix worked. You can still undo it. | Ich habe das neue Video geprüft: die Korrektur hat funktioniert. Du kannst sie weiterhin rückgängig machen. | Yeni videoyu kontrol ettim: düzeltme işe yaradı. İstersen hâlâ geri alabilirsin. |  |
| I confirmed where my clips go on the song timeline. | Ich habe bestätigt, wo meine Clips im Song liegen. | Kliplerimin şarkıda nereye geleceğini onayladım. |  |
| I could fit {planned} moment, not {count}. | Ich konnte {planned} Moment unterbringen, nicht {count}. | {count} değil, {planned} an sığdırabildim. |  |
| I could fit {planned} moments, not {count}. | Ich konnte {planned} Momente unterbringen, nicht {count}. | {count} değil, {planned} an sığdırabildim. |  |
| I could only find the speaker's face in part of the clip, not enough to fit one vertical crop around it | Ich konnte das Gesicht der sprechenden Person nur in einem Teil des Clips finden, zu wenig für einen Hochformat-Ausschnitt darum herum | Konuşan kişinin yüzünü klibin yalnızca bir kısmında bulabildim, tek bir dikey kadrajı ona göre ayarlamaya yetmedi |  |
| I couldn't add it at the end | Ich konnte es am Ende nicht hinzufügen | Sona ekleyemedim |  |
| I couldn't apply it | Ich konnte es nicht anwenden | uygulayamadım |  |
| I couldn't apply that change on your iPhone, so I left the video as it was. | Ich konnte diese Änderung auf deinem iPhone nicht anwenden, deshalb habe ich das Video so gelassen, wie es war. | Bu değişikliği iPhone'unda uygulayamadım, o yüzden videoyu olduğu gibi bıraktım. |  |
| I couldn't apply that edit, so your video is unchanged. | Ich konnte diese Änderung nicht anwenden, dein Video ist unverändert. | Bu düzenlemeyi uygulayamadım, videon değişmedi. |  |
| I couldn't apply that exact direction to this edit. Could you say what matters most, and I'll build around it? | Ich konnte diese genaue Vorgabe in diesem Edit nicht umsetzen. Kannst du sagen, was dir am wichtigsten ist, dann baue ich darum herum? | İstediğin yönü bu düzenlemeye aynen uygulayamadım. En çok neyin önemli olduğunu söyler misin, ona göre kurayım? |  |
| I couldn't apply that: {output_reply_notes} | Das konnte ich nicht anwenden: {output_reply_notes} | Bunu uygulayamadım: {output_reply_notes} |  |
| I couldn't build a valid draft change for that request. Try again. | Ich konnte für diese Anfrage keine gültige Entwurfsänderung erstellen. Versuch es nochmal. | Bu istek için geçerli bir taslak değişikliği oluşturamadım. Tekrar dene. |  |
| I couldn't build the confirmed camera-audio edit. | Ich konnte den bestätigten Edit mit Kameraton nicht bauen. | Onayladığın kamera sesli düzenlemeyi kuramadım. |  |
| I couldn't check the unclear word in this video | Ich konnte das unklare Wort in diesem Video nicht prüfen | Videodaki belirsiz kelimeyi kontrol edemedim |  |
| I couldn't check where the speaker's face is in the clip | Ich konnte nicht prüfen, wo das Gesicht der sprechenden Person im Clip ist | Klipte konuşan kişinin yüzünün nerede olduğunu kontrol edemedim |  |
| I couldn't check which language the captions are in. | Ich konnte nicht prüfen, in welcher Sprache die Untertitel sind. | Altyazıların dilini kontrol edemedim. |  |
| I couldn't confidently place that phrase in the original speech | Ich konnte diese Stelle nicht sicher in der Originalsprache zuordnen | O ifadeyi orijinal konuşmada güvenle yerleştiremedim |  |
| I couldn't confirm all the on-screen text in the finished video was readable (around {at:.0f}s). If a line looks off, tell me and I'll fix it. | Ich konnte nicht bestätigen, dass der ganze Text im fertigen Video lesbar war (etwa bei {at:.0f} s). Wenn eine Zeile komisch aussieht, sag mir Bescheid, dann korrigiere ich sie. | Bitmiş videodaki ekran yazılarının hepsinin okunaklı olduğunu doğrulayamadım (yaklaşık {at:.0f}. saniye). Bir satır yanlış görünüyorsa söyle, düzelteyim. |  |
| I couldn't confirm the clips are in a random order. | Ich konnte nicht bestätigen, dass die Clips in zufälliger Reihenfolge liegen. | Kliplerin rastgele sırada olduğunu doğrulayamadım. |  |
| I couldn't confirm the order your edit used. | Ich konnte die Reihenfolge, die dein Edit nutzt, nicht bestätigen. | Düzenlemenin kullandığı sırayı doğrulayamadım. |  |
| I couldn't confirm the part of the clip that plays shows the moment you named | Ich konnte nicht bestätigen, dass der abgespielte Teil des Clips den Moment zeigt, den du genannt hast | Oynayan klip parçasının söylediğin anı gösterdiğini doğrulayamadım |  |
| I couldn't confirm the part of the clip that plays shows the moment you named: {moments} | Ich konnte nicht bestätigen, dass der abgespielte Teil des Clips den Moment zeigt, den du genannt hast: {moments} | {v2}: {moments} |  |
| I couldn't do that one | Das eine konnte ich nicht machen | Bunu yapamadım |  |
| I couldn't do that. | Das konnte ich nicht machen. | Bunu yapamadım. |  |
| I couldn't do those. | Die konnte ich nicht machen. | Bunları yapamadım. |  |
| I couldn't double-check the speech on this clip, so I can't spot the unclear word | Ich konnte die Sprache in diesem Clip nicht doppelt prüfen, deshalb kann ich das unklare Wort nicht finden | Bu klipteki konuşmayı iki kez karşılaştıramadım, o yüzden belirsiz kelimeyi bulamıyorum |  |
| I couldn't find "{line}" in that clip, so it plays like the rest | Ich konnte "{line}" in dem Clip nicht finden, deshalb läuft er wie die anderen | "{line}" sözünü o klipte bulamadım, o yüzden diğerleri gibi çalıyor |  |
| I couldn't find "{quote}" in that clip | Ich konnte "{quote}" in dem Clip nicht finden | "{quote}" sözünü o klipte bulamadım |  |
| I couldn't find any "{work_intent_attribute}" to put the name placeholder on | Ich konnte kein "{work_intent_attribute}" finden, auf das ich den Namens-Platzhalter setzen kann | İsim yer tutucusunu koyacak "{work_intent_attribute}" klibi bulamadım |  |
| I couldn't find any clips for "{label}" | Ich konnte keine Clips für "{label}" finden | Şunun için klip bulamadım: "{label}" |  |
| I couldn't find clear speech in your clips to build this edit. | Ich habe in deinen Clips keine klare Sprache gefunden, um diesen Edit zu bauen. | Bu düzenlemeyi kurmak için kliplerinde net bir konuşma bulamadım. |  |
| I couldn't find every moment you named in your clip, so I can't confirm the ones playing are those moments | Ich konnte nicht jeden Moment, den du genannt hast, in deinem Clip finden, deshalb kann ich nicht bestätigen, dass die abgespielten diese Momente sind | Saydığın her anı klipte bulamadım, o yüzden oynayanların o anlar olduğunu doğrulayamıyorum |  |
| I couldn't find it in your take | ich habe es in deinem Take nicht gefunden | çekiminde bulamadım |  |
| I couldn't find that clip among your clips | Ich konnte diesen Clip unter deinen Clips nicht finden | O klibi kliplerin arasında bulamadım |  |
| I couldn't find that moment in clip {number}, so it plays at normal speed. I can slow down all of clip {number} instead | Ich konnte diesen Moment in Clip {number} nicht finden, deshalb läuft er im normalen Tempo. Ich kann stattdessen den ganzen Clip {number} verlangsamen | O anı {number}. klipte bulamadım, bu yüzden normal hızda oynuyor. İstersen {number}. klibin tamamını yavaşlatabilirim |  |
| I couldn't find that spoken phrase in the original audio | Ich konnte diese gesprochene Stelle im Originalton nicht finden | O konuşulan ifadeyi orijinal seste bulamadım |  |
| I couldn't find that video among your clips and Visuals | Ich konnte dieses Video weder unter deinen Clips noch in Visuals finden | O videoyu kliplerin ve Visuals arasında bulamadım |  |
| I couldn't find the closing badge you named | Ich konnte das Abschluss-Badge, das du genannt hast, nicht finden | Söylediğin kapanış rozetini bulamadım |  |
| I couldn't find the closing photo you named | Ich konnte das Abschlussfoto, das du genannt hast, nicht finden | Söylediğin kapanış fotoğrafını bulamadım |  |
| I couldn't find the moment you say “{quote}” in your clips, so the video can't end exactly on it. | Ich konnte den Moment, in dem du „{quote}“ sagst, in deinen Clips nicht finden, deshalb kann das Video nicht genau dort enden. | Kliplerinde “{quote}” dediğin anı bulamadım, bu yüzden video tam o anda bitemiyor. |  |
| I couldn't find the one you picked in your take | ich habe die Stelle, die du gewählt hast, in deinem Take nicht gefunden | seçtiğin anı çekiminde bulamadım |  |
| I couldn't find the photo or sticker for "{trigger}" | Ich konnte das Foto oder den Sticker für "{trigger}" nicht finden | "{trigger}" için fotoğraf ya da çıkartma bulamadım |  |
| I couldn't find the photo or sticker for {names_unresolved} | Ich konnte das Foto oder den Sticker für {names_unresolved} nicht finden | Şunlar için fotoğraf ya da çıkartma bulamadım: {names_unresolved} |  |
| I couldn't find the photo or sticker for {name}. | Ich konnte das Foto oder den Sticker für {name} nicht finden. | {name} için fotoğraf ya da çıkartma bulamadım. |  |
| I couldn't find the photos or stickers for {v1} | Ich konnte die Fotos oder Sticker für {v1} nicht finden | Şunlar için fotoğraf ya da çıkartma bulamadım: {v1} |  |
| I couldn't find the speaker's face anywhere in the clip | Ich konnte das Gesicht der sprechenden Person nirgends im Clip finden | Klipte konuşan kişinin yüzünü hiç bulamadım |  |
| I couldn't find the speaker's face reliably in the clip | Ich konnte das Gesicht der sprechenden Person im Clip nicht zuverlässig finden | Klipte konuşan kişinin yüzünü güvenle bulamadım |  |
| I couldn't find the {word} you picked in your take, so {text} isn't shown | Ich konnte das Wort {word}, das du gewählt hast, in deinem Take nicht finden, deshalb wird {text} nicht angezeigt | Seçtiğin {word} anını çekiminde bulamadım, o yüzden {text} yazısı çıkmadı |  |
| I couldn't find this moment in your clip, so I can't confirm it's the one playing | Ich habe diesen Moment in deinem Clip nicht gefunden, deshalb kann ich nicht bestätigen, dass es der ist, der läuft | Bu anı klipte bulamadım, o yüzden oynayanın o an olduğunu doğrulayamıyorum |  |
| I couldn't find where you say {quote} in your clips | Ich konnte nicht finden, wo du {quote} in deinen Clips sagst | {quote} dediğin yeri kliplerinde bulamadım |  |
| I couldn't find where you say “{quote}”, so the video doesn't end on it | Ich habe nicht gefunden, wo du „{quote}“ sagst, deshalb endet das Video nicht dort | “{quote}” dediğin yeri bulamadım, bu yüzden video o anda bitmiyor |  |
| I couldn't find where your takes sit in the song, so I used it as background music cut to the beat. To lip-sync, play the song out loud while filming, or sing along clearly so I can match your words (earbuds work, but the sync is a bit looser). | Ich habe nicht gefunden, wo deine Takes im Song liegen, deshalb habe ich ihn als Hintergrundmusik verwendet und auf den Beat geschnitten. Für Lip-Sync spiel den Song beim Filmen laut ab oder sing deutlich mit, damit ich deine Worte zuordnen kann (mit Kopfhörern geht es auch, aber die Synchronität ist etwas lockerer). | Çekimlerinin şarkının neresine denk geldiğini bulamadım, o yüzden şarkıyı ritme göre kesilmiş fon müziği olarak kullandım. Dudak senkronu için çekim yaparken şarkıyı yüksek sesle çal ya da sözleri net bir şekilde söyleyerek eşlik et, böylece sözlerini eşleştirebilirim (kulaklıkla da olur ama senkron biraz daha gevşek olur). |  |
| I couldn't find {describe_selector_selector}, so I changed nothing. Which text did you mean? | Ich habe {describe_selector_selector} nicht gefunden, deshalb habe ich nichts geändert. Welchen Text meinst du? | {describe_selector_tr_selector} bulamadım, bu yüzden hiçbir şeyi değiştirmedim. Hangi yazıyı kastettin? |  |
| I couldn't find {line} in your take, so the video runs to the end | Ich habe {line} in deinem Take nicht gefunden, deshalb läuft das Video bis zum Ende | {line} cümlesini çekiminde bulamadım, bu yüzden video sonuna kadar sürüyor |  |
| I couldn't find {line} in your take, so the video still opens at the beginning | Ich habe {line} in deinem Take nicht gefunden, deshalb beginnt das Video weiterhin am Anfang | {line} cümlesini çekiminde bulamadım, bu yüzden video hâlâ başından açılıyor |  |
| I couldn't find {missing} in your clip | Ich habe {missing} in deinem Clip nicht gefunden | Klibinde şunu bulamadım: {missing} |  |
| I couldn't find {moment_words} in clip {n} on my own, so clip {n} plays from {start:.1f} s to {end:.1f} s and may not show it. | Ich habe {moment_words} in Clip {n} nicht selbst gefunden, deshalb läuft Clip {n} von {start:.1f} s bis {end:.1f} s und zeigt es vielleicht nicht. | {n}. klipte “{moment_words}” kısmını kendim bulamadım; {n}. klibin {start:.1f}-{end:.1f} sn arası oynuyor ve bunu göstermeyebilir. |  |
| I couldn't find {name} in your Visuals for the small card | Ich habe {name} in deinen Visuals für die kleine Karte nicht gefunden | Küçük kart için {name} görsellerinde bulamadım |  |
| I couldn't find {shown} in your clips. Tap the clip that shows it, or tell me there isn't one. | Ich habe {shown} in deinen Clips nicht gefunden. Tipp auf den Clip, der es zeigt, oder sag mir, dass es keinen gibt. | {shown} kliplerinde bulamadım. Onu gösteren klibe dokun ya da olmadığını söyle. |  |
| I couldn't find {shown}{more} in your clips. Tap the clip for each one, or mark the ones that aren't there. | Ich habe {shown}{more} in deinen Clips nicht gefunden. Tipp bei jedem auf den passenden Clip oder markier die, die nicht vorkommen. | Şunları kliplerinde bulamadım: {shown}{more}. Her biri için klibe dokun ya da olmayanları işaretle. |  |
| I couldn't keep something you asked for in this edit. | Ich konnte etwas, das du dir gewünscht hast, in diesem Edit nicht umsetzen. | İstediğin bir şeyi bu düzenlemede koruyamadım. |  |
| I couldn't keep the clip order you asked for. | Ich konnte die gewünschte Clip-Reihenfolge nicht beibehalten. | İstediğin klip sırasını koruyamadım. |  |
| I couldn't keep the speaker's face in one vertical crop | Ich konnte das Gesicht der sprechenden Person nicht in einem Hochformat-Ausschnitt halten | Konuşan kişinin yüzünü tek bir dikey kadrajda tutamadım |  |
| I couldn't leave out clip {numbers}, so it is still in the video. | Ich konnte Clip {numbers} nicht weglassen, deshalb ist er noch im Video. | {clips_tr} çıkaramadım, o yüzden videoda duruyor. |  |
| I couldn't leave out clips {numbers}, so they are still in the video. | Ich konnte die Clips {numbers} nicht weglassen, deshalb sind sie noch im Video. | {clips_tr} çıkaramadım, o yüzden videoda duruyorlar. |  |
| I couldn't line each clip up with the words you say, so the clips play in order and share your voiceover evenly | Ich konnte nicht jeden Clip zu deinen Worten zuordnen, deshalb laufen die Clips der Reihe nach und teilen sich dein Voiceover gleichmäßig | Her klibi söylediğin kelimelerle eşleştiremedim, bu yüzden klipler sırayla oynuyor ve seslendirmeni eşit paylaşıyor |  |
| I couldn't line your clips up with your song. Film with the song playing out loud, or sing along clearly, then try again. | Ich konnte deine Clips nicht auf deinen Song abstimmen. Filme, während der Song laut läuft, oder sing deutlich mit, und versuch es dann nochmal. | Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya da sözleri net söyleyerek eşlik et, sonra tekrar dene. |  |
| I couldn't locate the unclear word reliably. Which caption word or line do you mean, and where is it in the clip? Give its time and the wording, or identify the part to leave out. | Ich konnte das unklare Wort nicht sicher finden. Welches Untertitel-Wort oder welche Zeile meinst du, und wo im Clip ist es? Gib mir die Zeit und den Wortlaut oder sag mir, welcher Teil wegfallen soll. | Anlaşılmayan kelimenin yerini güvenle bulamıyorum. Hangi altyazı kelimesini ya da cümlesini kastediyorsun ve klibin neresinde? Zamanını ve yazılacak kelimeleri ya da çıkarılacak kısmı söyle. |  |
| I couldn't make out one word around {at} in your clip: the two listens heard different words, and I won't guess. | Ich habe ein Wort bei {at} in deinem Clip nicht verstanden: Beim zweimaligen Anhören habe ich verschiedene Wörter gehört, und ich rate nicht. | Klibinde {at} civarında bir kelimeyi anlayamadım: klibini iki kez dinledim, farklı kelimeler duydum ve tahmin yürütmeyeceğim. |  |
| I couldn't make that change on this edit. | Ich konnte diese Änderung an diesem Edit nicht vornehmen. | Bu değişikliği bu düzenlemede yapamadım. |  |
| I couldn't make the image this time. Nothing in the edit changed. | Ich konnte das Bild diesmal nicht erstellen. Am Edit hat sich nichts geändert. | Bu sefer görseli yapamadım. Düzenlemede hiçbir şey değişmedi. |  |
| I couldn't make the video yet: you asked for a title but gave no words. | Ich konnte das Video noch nicht erstellen: Du wolltest einen Titel, hast mir aber keinen Text gegeben. | Videoyu henüz oluşturamadım: başlık istedin ama kelimelerini vermedin. |  |
| I couldn't make your last video with this choice. | Ich konnte dein letztes Video mit dieser Auswahl nicht erstellen. | Son videonu bu seçimle oluşturamadım. |  |
| I couldn't match that request to your clips automatically — can you tell me which clips you mean? | Ich konnte diese Anfrage nicht automatisch deinen Clips zuordnen – kannst du mir sagen, welche Clips du meinst? | Bu isteği kliplerinle otomatik olarak eşleştiremedim. Hangi klipleri kastettiğini söyler misin? |  |
| I couldn't match the narration to the actions reliably. Your draft is saved. Should I try again or use a simpler clip sequence? | Ich konnte die Erzählung nicht sicher den Szenen zuordnen. Dein Entwurf ist gespeichert. Soll ich es nochmal versuchen oder eine einfachere Clip-Reihenfolge nehmen? | Seslendirmeyi kliplerle güvenilir bir şekilde eşleştiremedim. Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa daha basit bir klip sırası mı kullanayım? |  |
| I couldn't match your description to the clips, so they stay in the order you attached them. | Ich konnte deine Beschreibung nicht den Clips zuordnen, deshalb bleiben sie in der Reihenfolge, in der du sie hinzugefügt hast. | Anlattığını kliplerle eşleştiremedim, o yüzden klipler eklediğin sırada kaldı. |  |
| I couldn't measure the quiet parts of some of your takes, so I didn't shorten their shorter pauses | Ich konnte die leisen Stellen in einigen deiner Takes nicht messen, deshalb habe ich dort die kürzeren Pausen nicht gekürzt | Bazı çekimlerindeki sessiz kısımları ölçemedim, o yüzden onların kısa duraklamalarını kısaltmadım |  |
| I couldn't measure the quiet parts of your take, so I didn't shorten the shorter pauses | Ich konnte die leisen Stellen in deinem Take nicht messen, deshalb habe ich die kürzeren Pausen nicht gekürzt | Çekimindeki sessiz kısımları ölçemedim, o yüzden kısa duraklamaları kısaltmadım |  |
| I couldn't open your current edit to change it in place. Try again, or ask me for a new version. | Ich konnte deinen aktuellen Edit nicht öffnen, um ihn direkt zu ändern. Versuch es nochmal oder bitte mich um eine neue Version. | Şu anki düzenlemeni açıp yerinde değiştiremedim. Tekrar dene ya da benden yeni bir sürüm iste. |  |
| I couldn't open your current video to update it. Try again in a moment. | Ich konnte dein aktuelles Video nicht öffnen, um es zu aktualisieren. Versuch es gleich nochmal. | Şu anki videonu güncellemek için açamadım. Birazdan tekrar dene. |  |
| I couldn't place everything you listed in the order you gave. | Ich konnte nicht alles, was du aufgezählt hast, in deiner Reihenfolge platzieren. | Saydığın her şeyi verdiğin sıraya yerleştiremedim. |  |
| I couldn't place that shot from my notes on your clips, so I haven't ruled them out. If it's in one of them ({names}), tell me which one and about when it happens. Or tell me to build the edit without it. | Ich konnte diese Aufnahme anhand meiner Notizen zu deinen Clips nicht einordnen, deshalb habe ich keinen davon ausgeschlossen. Wenn sie in einem davon ist ({names}), sag mir welcher und ungefähr wann sie vorkommt. Oder sag mir, dass ich den Edit ohne sie erstellen soll. | O çekimi kliplerin hakkındaki notlarımdan yerleştiremedim, bu yüzden kliplerini elemedim. Birindeyse ({names}) hangisi olduğunu ve yaklaşık ne zaman geçtiğini söyle. Ya da videoyu onsuz oluşturmamı söyle. |  |
| I couldn't place the small card of {name} without covering a face | Ich konnte die kleine Karte von {name} nicht platzieren, ohne ein Gesicht zu verdecken | {name} küçük kartını bir yüzü kapatmadan yerleştiremedim |  |
| I couldn't place {text} on this video | Ich konnte {text} in diesem Video nicht platzieren | {text} yazısını bu videoya yerleştiremedim |  |
| I couldn't play it in the middle of your talk | Ich konnte es nicht mitten in deiner Rede abspielen | Konuşmanın ortasında oynatamadım |  |
| I couldn't play that clip's own sound in this edit | Ich konnte den Originalton dieses Clips in diesem Edit nicht abspielen | O klibin kendi sesini bu düzenlemede çalamadım |  |
| I couldn't prepare that video on your iPhone | Ich konnte dieses Video auf deinem iPhone nicht vorbereiten | O videoyu iPhone'unda hazırlayamadım |  |
| I couldn't prepare the text while keeping your approved wording. Your draft and footage are saved. Try generating this edit again. | Ich konnte den Text nicht vorbereiten, ohne deinen freigegebenen Wortlaut zu ändern. Dein Entwurf und dein Material sind gespeichert. Lass diesen Edit nochmal erstellen. | Onayladığın yazıyı koruyarak metni hazırlayamadım. Taslağın ve çekimlerin kayıtlı. Bu videoyu yeniden oluşturmayı dene. |  |
| I couldn't prepare those edits this time. | Ich konnte diese Änderungen diesmal nicht vorbereiten. | Bu sefer bu düzenlemeleri hazırlayamadım. |  |
| I couldn't put your own words on it | Ich konnte deine eigenen Worte nicht draufsetzen | Senin sözlerini üstüne koyamadım |  |
| I couldn't read the speech in your clip. | Ich konnte die Sprache in deinem Clip nicht erfassen. | Klibindeki konuşmayı okuyamadım. |  |
| I couldn't reliably match that request to your clips. Please try again shortly. | Ich konnte diese Anfrage nicht sicher deinen Clips zuordnen. Versuch es bitte gleich nochmal. | Bu isteği kliplerinle güvenilir şekilde eşleştiremedim. Birazdan tekrar dene. |  |
| I couldn't reliably read every requested change. | Ich konnte nicht jede gewünschte Änderung sicher erfassen. | İstediğin her değişikliği güvenilir şekilde okuyamadım. |  |
| I couldn't resolve all requested clip matches. Could you clarify? | Ich konnte nicht alle gewünschten Clip-Zuordnungen klären. Kannst du das genauer erklären? | İstediğin klip eşleşmelerinin hepsini çözemedim. Biraz açıklar mısın? |  |
| I couldn't safely apply that change. Nothing was changed. | Ich konnte diese Änderung nicht sicher anwenden. Es wurde nichts geändert. | Bu değişikliği güvenle uygulayamadım. Hiçbir şey değişmedi. |  |
| I couldn't safely place {words} on the recorded words. Check the phrase or choose a different moment. | Ich konnte {words} nicht sicher auf die aufgenommenen Worte setzen. Prüf die Stelle oder wähl einen anderen Moment. | {words} yazısını kayıttaki sözlere yerleştiremedim. Sözü kontrol et ya da başka bir an seç. |  |
| I couldn't safely verify the clip-specific instructions. Please restate which clips to use, group, order, label, or caption. | Ich konnte die Anweisungen zu einzelnen Clips nicht sicher prüfen. Sag mir bitte nochmal, welche Clips ich verwenden, gruppieren, sortieren, beschriften oder mit Untertiteln versehen soll. | Klip talimatlarını güvenle doğrulayamadım. Hangi klipleri kullanacağımı, gruplayacağımı, sıralayacağımı, etiketleyeceğimi ya da yazı ekleyeceğimi yeniden yazar mısın? |  |
| I couldn't safely verify {named} | Ich konnte {named} nicht sicher prüfen | Şunları güvenle doğrulayamadım: {named} |  |
| I couldn't set the effect to {value!r}; the options are· | Ich konnte den Effekt nicht auf {value!r} setzen; die Optionen sind:· | Efekti {value!r} yapamadım; seçenekler:· |  |
| I couldn't set the letter case to {value!r}; the options are none, upper, lower, title | Ich konnte die Groß-/Kleinschreibung nicht auf {value!r} setzen; die Optionen sind none, upper, lower, title | Harf biçimini {value!r} yapamadım; seçenekler: none, upper, lower, title |  |
| I couldn't set up showing your other clips full screen over your talk this time. Send the request again, or tell me which clip should cover which part, and I'll try once more. | Ich konnte diesmal nicht einrichten, deine anderen Clips im Vollbild über deine Rede zu legen. Schick die Anfrage nochmal oder sag mir, welcher Clip welchen Teil abdecken soll, dann versuche ich es nochmal. | Diğer kliplerini konuşmanın üzerinde tam ekran göstermeyi bu sefer hazırlayamadım. İsteği tekrar gönder ya da hangi klibin hangi kısmı kaplayacağını söyle, bir kez daha deneyeyim. |  |
| I couldn't start the render. Your draft is still saved, so you can retry without repeating the edit. | Ich konnte das Video nicht starten. Dein Entwurf ist weiterhin gespeichert, du kannst es also erneut versuchen, ohne den Edit zu wiederholen. | Videoyu başlatamadım. Taslağın hâlâ kayıtlı, düzenlemeyi baştan anlatmadan tekrar deneyebilirsin. |  |
| I couldn't suggest a {join_names} track this time. | Diesmal konnte ich dir keinen Song im Stil {join_names} vorschlagen. | Bu sefer bir {join_names_2} şarkısı öneremedim. |  |
| I couldn't suggest an {join_names} track this time. | Diesmal konnte ich dir keinen Song im Stil {join_names} vorschlagen. | Bu sefer bir {join_names_2} şarkısı öneremedim. |  |
| I couldn't tell by meaning which sentence {clip} is about, so I matched it by its words | Ich konnte dem Sinn nach nicht erkennen, um welchen Satz es bei {clip} geht, deshalb habe ich es über die Wörter zugeordnet | {clip} için hangi cümlenin ilgili olduğunu anlamdan çıkaramadım, kelimelerinden eşledim |  |
| I couldn't tell exactly where "{quote}" is said in that clip | Ich konnte nicht genau erkennen, wo in diesem Clip "{quote}" gesagt wird | "{quote}" sözünün o klipte tam nerede söylendiğini anlayamadım |  |
| I couldn't tell if these are "{work_intent_attribute}" | Ich konnte nicht erkennen, ob diese "{work_intent_attribute}" sind | Şunların "{work_intent_attribute}" olup olmadığını anlayamadım |  |
| I couldn't tell the {work_intent_attribute} | Ich konnte {work_intent_attribute} nicht erkennen | Anlayamadım: {work_intent_attribute} |  |
| I couldn't tell which clip "{literal}" goes on, so I can't confirm that text. | Ich konnte nicht erkennen, auf welchen Clip "{literal}" gehört, deshalb kann ich diesen Text nicht bestätigen. | "{literal}" yazısının hangi klibe gideceğini anlayamadım, o yüzden bu yazıyı doğrulayamıyorum. |  |
| I couldn't tell which clip "{words}" means, so I can't confirm the order. | Ich konnte nicht erkennen, welchen Clip du mit "{words}" meinst, deshalb kann ich die Reihenfolge nicht bestätigen. | "{words}" derken hangi klibi kastettiğini anlayamadım, o yüzden sırayı doğrulayamıyorum. |  |
| I couldn't tell which clip is on screen when you say {v1} | Ich konnte nicht erkennen, welcher Clip im Bild ist, wenn du {v1} sagst | {v1} dediğinde ekranda hangi klibin olduğunu anlayamadım |  |
| I couldn't tell which clip you meant for the speed change, so the clips play at normal speed. Tell me which clip | Ich konnte nicht erkennen, welchen Clip du für die Tempoänderung meinst, deshalb laufen die Clips in normalem Tempo. Sag mir, welcher Clip | Hız değişikliği için hangi klibi kastettiğini anlayamadım, o yüzden klipler normal hızda oynuyor. Hangi klip olduğunu yaz |  |
| I couldn't tell which clips | Ich konnte nicht erkennen, welche Clips gemeint sind | Hangi kliplerden söz ettiğini anlayamadım |  |
| I couldn't tell which clips "{words}" means, so I can't confirm the order. | Ich konnte nicht erkennen, welche Clips du mit "{words}" meinst, deshalb kann ich die Reihenfolge nicht bestätigen. | "{words}" derken hangi klipleri kastettiğini anlayamadım, o yüzden sırayı doğrulayamıyorum. |  |
| I couldn't tell which clips are in this edit, so I couldn't keep their order. | Ich konnte nicht erkennen, welche Clips in diesem Edit sind, deshalb konnte ich ihre Reihenfolge nicht beibehalten. | Bu düzenlemede hangi kliplerin olduğunu anlayamadım, o yüzden sıralarını koruyamadım. |  |
| I couldn't tell which clips you asked me to leave out, so I haven't removed any for that. Tell me which ones and I'll take them out. | Ich konnte nicht erkennen, welche Clips ich weglassen soll, deshalb habe ich dafür keinen entfernt. Sag mir welche, dann nehme ich sie raus. | Hangi klipleri çıkarmamı istediğini anlayamadım, bu yüzden o istek için hiçbirini çıkarmadım. Hangilerini çıkarayım, söylersen çıkarırım. |  |
| I couldn't tell which clips you want first or last, so I can't confirm their order. | Ich konnte nicht erkennen, welche Clips du am Anfang oder am Ende haben möchtest, deshalb kann ich ihre Reihenfolge nicht bestätigen. | Hangi klipleri başa ya da sona istediğini anlayamadım, o yüzden sıralarını doğrulayamıyorum. |  |
| I couldn't tell which clips you want first or last. | Ich konnte nicht erkennen, welche Clips du am Anfang oder am Ende haben möchtest. | Hangi klipleri başa ya da sona istediğini anlayamadım. |  |
| I couldn't tell which moment to freeze, so that moment plays normally. Tell me what happens in the moment to hold, like who is in it and what they do | Ich konnte nicht erkennen, welchen Moment ich einfrieren soll, deshalb läuft er normal. Sag mir, was in dem Moment passiert, zum Beispiel wer drin ist und was die Person macht | Hangi anı donduracağımı anlayamadım, o yüzden o an normal akıyor. Hangi klipte kaçıncı saniyeyi tutayım, yaz |  |
| I couldn't tell which of your clips match {shown}{more}. Tap the clips for each one, or mark the ones that have none. | Ich konnte nicht erkennen, welche deiner Clips zu {shown}{more} passen. Tipp bei jedem auf die passenden Clips oder markier die, zu denen es keinen gibt. | Şunlar için hangi klipleri kastettiğini anlayamadım: {shown}{more}. Her biri için klipleri seç ya da hiç klibi olmayanları işaretle. |  |
| I couldn't tell which of your clips show "{labels_0}". Tap the clips that do, or tell me there aren't any. | Ich konnte nicht erkennen, welche deiner Clips "{labels_0}" zeigen. Tipp auf die passenden Clips oder sag mir, dass es keine gibt. | Kliplerinden hangilerinde "{labels_0}" olduğunu anlayamadım. Olanlara dokun ya da hiç olmadığını söyle. |  |
| I couldn't tell which of your clips to leave out for "{labels_0}". Tap the ones to leave out, or tell me none. | Ich konnte nicht erkennen, welche deiner Clips ich für "{labels_0}" weglassen soll. Tipp auf die, die wegfallen sollen, oder sag mir, dass keiner wegfällt. | "{labels_0}" için hangi klipleri çıkaracağımı anlayamadım. Çıkarılacak olanlara dokun ya da hiçbirini çıkarmamamı söyle. |  |
| I couldn't tell which of your clips to leave out for {shown}{more}. Tap the clips to leave out for each one, or mark the ones with none. | Ich konnte nicht erkennen, welche deiner Clips ich für {shown}{more} weglassen soll. Tipp bei jedem auf die Clips, die wegfallen sollen, oder markier die, bei denen keiner wegfällt. | Şunlar için hangi klipleri çıkaracağımı anlayamadım: {shown}{more}. Her biri için çıkarılacak klipleri seç ya da hiçbirini çıkarmayacaklarını işaretle. |  |
| I couldn't tell which sentence {clip} is about, so it plays at my best guess | Ich konnte nicht erkennen, um welchen Satz es bei {clip} geht, deshalb läuft er an der Stelle, die ich am wahrscheinlichsten halte | {clip} için hangi cümlenin ilgili olduğunu çıkaramadım, en iyi tahminimde oynuyor |  |
| I couldn't tell which spoken word to wait for, so tell me the exact word to show it on | Ich konnte nicht erkennen, auf welches gesprochene Wort ich warten soll, sag mir also das genaue Wort, bei dem er erscheinen soll | Hangi kelimeyi beklemem gerektiğini anlayamadım; yazının çıkacağı kelimeyi tam olarak söyle |  |
| I couldn't tell which words to check the spelling of. | Ich konnte nicht erkennen, bei welchen Wörtern ich die Schreibweise prüfen soll. | Yazımını kontrol edeceğim kelimeleri ayırt edemedim. |  |
| I couldn't turn that into a plan this time. | Ich konnte daraus diesmal keinen Plan machen. | Bu sefer bunu bir plana dönüştüremedim. |  |
| I couldn't write a script from your clips right now. Ask me again, or record or import your own voiceover and I'll plan the edit around it. | Ich konnte gerade kein Skript aus deinen Clips schreiben. Frag mich nochmal oder nimm dein eigenes Voiceover auf oder importier es, dann plane ich den Edit darum herum. | Şu anda kliplerinden bir metin yazamadım. Tekrar sor ya da kendi seslendirmeni kaydet veya içe aktar, düzenlemeyi ona göre planlayayım. |  |
| I couldn't zoom into all of your recordings, so small text may stay small. | Ich konnte nicht in alle deine Aufnahmen hineinzoomen, deshalb bleibt kleiner Text vielleicht klein. | Kayıtların hepsine yakınlaşamadım, küçük yazılar küçük kalabilir. |  |
| I couldn't {action}: {why}. | {action} hat nicht geklappt: {why}. | {action} mümkün olmadı: {why}. |  |
| I couldn’t apply that uploaded Visuals removal. Nothing was changed. I can remove supported uploaded image and video Visuals; please identify the ones you want removed. | Ich konnte das Entfernen der hochgeladenen Visuals nicht anwenden. Es wurde nichts geändert. Ich kann hochgeladene Bild- und Video-Visuals entfernen; sag mir bitte, welche weg sollen. | Görselleri kaldırma isteğini uygulayamadım. Hiçbir şey değişmedi. Yüklediğin fotoğraf ve videoları kaldırabilirim; hangilerini kaldırmamı istediğini söyle. |  |
| I couldn’t safely save that Visuals removal. The edit is unchanged. | Ich konnte das Entfernen der Visuals nicht sicher speichern. Der Edit ist unverändert. | Görselleri kaldırma işlemini güvenle kaydedemedim. Kurgu aynı kaldı. |  |
| I cut this as a fast montage instead of a guided story: {v1}. | Ich habe das als schnelle Montage statt als geführte Story geschnitten: {v1}. | Bunu {label_tr} yerine hızlı montaj olarak kestim: {v2}. |  |
| I cut this as a fast montage instead of a text explainer: {v1}. | Ich habe das als schnelle Montage statt als Text-Erklärvideo geschnitten: {v1}. | Bunu {label_tr} yerine hızlı montaj olarak kestim: {v2}. |  |
| I cut this as a fast montage instead of a {label_en}: {v1}. | Ich habe das als schnelle Montage statt als {label_en} geschnitten: {v1}. | Bunu {label_tr} yerine hızlı montaj olarak kestim: {v2}. |  |
| I did {completed} of your {total} asks. | Ich habe {completed} von deinen {total} Wünschen umgesetzt. | {total} isteğinden {completed} tanesini yaptım. |  |
| I didn't add a title because no creator text or grounded brief facts were available. | Ich habe keinen Titel hinzugefügt, weil weder ein Text von dir noch belegbare Fakten aus deinem Briefing vorlagen. | Başlık eklemedim, çünkü kullanabileceğim bir yazın ya da isteğinde dayanabileceğim bir bilgi yoktu. |  |
| I didn't caption clip {join_skipped}. | Clip {join_skipped} habe ich nicht untertitelt. | Klip {join_skipped} için yazı eklemedim. |  |
| I didn't caption clips {join_skipped}. | Die Clips {join_skipped} habe ich nicht untertitelt. | Klip {join_skipped} için yazı eklemedim. |  |
| I didn't hear where that part starts, so the label isn't in | Ich habe nicht gehört, wo dieser Teil beginnt, deshalb ist das Label nicht drin | O bölümün nerede başladığını duymadım, o yüzden etiket yok |  |
| I didn't hear you say that in your talk, and nothing you say is about the clip, so I left it out | Ich habe nicht gehört, dass du das in deiner Rede sagst, und nichts von dem, was du sagst, passt zum Clip, deshalb habe ich ihn weggelassen | Konuşmanda bunu söylediğini duymadım ve anlattığın hiçbir şey klibe uymuyor, o yüzden dışarıda bıraktım |  |
| I didn't hear you say the words you named for {clip}, so it plays where you talk about it | Ich habe nicht gehört, dass du die Wörter sagst, die du für {clip} genannt hast, deshalb läuft er dort, wo du darüber sprichst | {clip} için söylediğin sözleri duymadım, o yüzden onu anlattığın yerde oynatıyorum |  |
| I didn't hear you say the words you named for {ref} and nothing you say is about it, so I left it out | Ich habe nicht gehört, dass du die Wörter sagst, die du für {ref} genannt hast, und nichts von dem, was du sagst, passt dazu, deshalb habe ich es weggelassen | {ref} için söylediğin sözleri duymadım ve anlattığın hiçbir şey ona uymuyor, o yüzden dışarıda bıraktım |  |
| I didn't hear you say {word} in your take, so {text} isn't shown | Ich habe nicht gehört, dass du {word} in deinem Take sagst, deshalb wird {text} nicht gezeigt | Çekiminde {word} dediğini duymadım, o yüzden {text} yazısı çıkmadı |  |
| I don't have a "{clip_value}" animation for the title yet, so it won't be used | Ich habe noch keine "{clip_value}"-Animation für den Titel, deshalb wird sie nicht verwendet | Başlık için henüz "{clip_value}" animasyonum yok, o yüzden kullanılmayacak |  |
| I don't have a font called {value!r} | Ich habe keine Schriftart namens {value!r} | {value!r} adında bir yazı tipim yok |  |
| I don't have a way to do that here | Dafür habe ich hier keine Möglichkeit | bunu burada yapmanın bir yolu yok |  |
| I don't have any {join_names} tracks. The closest I have is {song_en}. Add it when you post if you like it. | Ich habe keine Songs im Stil {join_names}. Am nächsten kommt {song_en}. Füg ihn beim Posten hinzu, wenn er dir gefällt. | Elimde {join_names_2} şarkısı yok. En yakını {song_tr}. Beğenirsen paylaşırken ekleyebilirsin. |  |
| I ended the edit where your voice ends. | Ich habe den Edit dort beendet, wo deine Stimme endet. | Düzenlemeyi sesinin bittiği yerde bitirdim. |  |
| I extended it so every clip stays on screen long enough to be seen. | Ich habe es verlängert, damit jeder Clip lang genug zu sehen ist. | Süreyi uzattım, böylece her klip görülecek kadar ekranda kalıyor. |  |
| I found more than one matching phrase, so tell me which occurrence to cut | Ich habe mehr als eine passende Stelle gefunden, sag mir also, welche ich schneiden soll | Birden fazla eşleşen ifade buldum; hangisini kesmem gerektiğini söyle |  |
| I found no clips for {and_names_facts_sequence_absent}. | Ich habe keine Clips für {and_names_facts_sequence_absent} gefunden. | {and_names_facts_sequence_absent} için klip bulamadım. |  |
| I found no clips for: {join_facts_sequence_absent} | Ich habe keine Clips gefunden für: {join_facts_sequence_absent} | şunlar için klip bulamadım: {join_facts_sequence_absent} |  |
| I found no clips of it | Ich habe keine Clips davon gefunden | Bununla ilgili klip bulamadım |  |
| I found repeated trigger words for {words}. Tell me first, last, or which occurrence. | Ich habe die Auslösewörter für {words} mehrfach gefunden. Sag mir: das erste, das letzte oder welches Mal. | {words} için beklenen sözler tekrarlanıyor. İlk, son ya da hangi tekrar olduğunu söyle. |  |
| I found that moment | Ich habe diesen Moment gefunden | O anı buldum |  |
| I found that moment ({start}–{end}s into the clip) but not the exact frame to hold, so it plays normally. Tell me the second to freeze on, like "freeze at {middle} seconds" | Ich habe diesen Moment gefunden ({start}–{end} s im Clip), aber nicht das genaue Bild zum Anhalten, deshalb läuft er normal. Sag mir, bei welcher Sekunde das Bild stehen soll, zum Beispiel „bei {middle} Sekunden einfrieren“ | {v1} (klibin {start}–{end}. saniyeleri) ama tutulacak tam kareyi bilmiyorum, o yüzden normal oynuyor. Hangi saniyede donsun, söyle; örneğin "{middle}. saniyede dondur" |  |
| I found where you say “{quote}”, but couldn't fit it as the ending with your other clips, so the video won't end exactly on it. | Ich habe gefunden, wo du „{quote}“ sagst, konnte es aber nicht als Ende mit deinen anderen Clips unterbringen, deshalb endet das Video nicht genau dort. | “{quote}” dediğin anı buldum ama diğer kliplerle birlikte sona yerleştiremedim, bu yüzden video tam o anda bitmeyecek. |  |
| I found {found} of your {total} moments in the clip. Put them in order and show me where each one is, and I'll cut exactly those. | Ich habe {found} von deinen {total} Momenten im Clip gefunden. Bring sie in die richtige Reihenfolge und zeig mir, wo jeder ist, dann schneide ich genau diese. | Saydığın {total} andan {found} tanesini klipte buldum. Onları sıraya koy ve her birinin nerede olduğunu göster, tam olarak onları keseyim. |  |
| I found {line} but cutting to it would leave too little video, so the video still opens at the beginning of your take | Ich habe {line} gefunden, aber ein Schnitt dorthin würde zu wenig Video übrig lassen, deshalb beginnt das Video weiterhin am Anfang deines Takes | {line} cümlesini buldum ama oraya kesmek çok az video bırakırdı, bu yüzden video hâlâ çekimin başından açılıyor |  |
| I found {line} but ending there would leave too little video, so the video runs to the end of your take | Ich habe {line} gefunden, aber dort zu enden würde zu wenig Video übrig lassen, deshalb läuft das Video bis zum Ende deines Takes | {line} cümlesini buldum ama orada bitirmek çok az video bırakırdı, bu yüzden video çekimin sonuna kadar sürüyor |  |
| I guessed these, tell me if any is wrong: {shown} (text I took from the footage, not from your words) | Das habe ich geraten, sag mir, wenn etwas nicht stimmt: {shown} (Text aus dem Material, nicht aus deinen Worten) | Şunları tahmin ettim, yanlış olan varsa söyle: {shown} (senin sözlerinden değil, çekimlerden aldığım yazılar) |  |
| I had nothing to go on for {clip}, so it plays spread across your talk | Für {clip} hatte ich keinen Anhaltspunkt, deshalb läuft er verteilt über deine Rede | {clip} hakkında elimde bir şey yoktu, konuşmana yayarak oynatıyorum |  |
| I haven't checked how the names are spelled. | Ich habe nicht geprüft, wie die Namen geschrieben werden. | İsimlerin yazımını kontrol etmedim. |  |
| I haven't established which renderer can make this edit yet. | Ich weiß noch nicht, welcher Renderer diesen Edit erstellen kann. | Bu düzenlemeyi hangi oluşturucunun yapabileceğini henüz belirleyemedim. |  |
| I heard {word} more than once. Choose which occurrence should show {text}. | Ich habe {word} mehr als einmal gehört. Wähl aus, bei welchem Mal {text} erscheinen soll. | {word} birden fazla kez geçiyor. {text} yazısının hangi tekrarda görüneceğini seç. |  |
| I held this first draft back. | Ich habe diesen ersten Entwurf zurückgehalten. | Bu ilk taslağı şimdilik beklettim. |  |
| I inspected the project, but I need footage evidence before editing. | Ich habe das Projekt angesehen, brauche aber Hinweise aus deinem Material, bevor ich etwas bearbeite. | Projeyi inceledim ama düzenlemeden önce çekimlerini görmem gerekiyor. |  |
| I kept the text away from where {named} was, but lost sight of it for part of the video, so I can't confirm it stays clear | Ich habe den Text von der Stelle ferngehalten, wo {named} war, habe es aber in einem Teil des Videos aus den Augen verloren, deshalb kann ich nicht bestätigen, dass der Bereich frei bleibt | Yazıyı {named_tr} olduğu yerden uzak tuttum ama videonun bir kısmında onu göremedim, açıkta kaldığını doğrulayamıyorum |  |
| I kept the zoom between {min_chat_focus_zoom:g}x and {max_chat_focus_zoom:g}x. | Ich habe den Zoom zwischen {min_chat_focus_zoom:g}x und {max_chat_focus_zoom:g}x gehalten. | Yakınlaştırmayı {min_chat_focus_zoom:g}x ile {max_chat_focus_zoom:g}x arasında tuttum. |  |
| I kept your length and used fewer clips, spread evenly through your footage. | Ich habe deine Länge beibehalten und weniger Clips verwendet, gleichmäßig über dein Material verteilt. | Süreni korudum ve daha az klip kullandım; klipleri çekimlerine eşit aralıklarla dağıttım. |  |
| I kept your length: the last seconds play without voice. | Ich habe deine Länge beibehalten: Die letzten Sekunden laufen ohne Stimme. | Süreni korudum: son saniyeler sesin olmadan oynuyor. |  |
| I kept your length: the photos you timed play at the edit's own pace. | Ich habe deine Länge beibehalten: Die Fotos, für die du eine Dauer festgelegt hast, laufen im eigenen Tempo des Edits. | Süreni korudum: süre verdiğin fotoğraflar düzenlemenin kendi hızında oynuyor. |  |
| I left out clip {join_unseen}: I can't tell what it shows. Tell me and I'll add it. | Clip {join_unseen} habe ich weggelassen: Ich erkenne nicht, was er zeigt. Sag es mir, dann ergänze ich ihn. | Klip {join_unseen} dışarıda kaldı: ne gösterdiğini anlayamadım. Söylersen ekleyeyim. |  |
| I left out clip {numbers} because it is too short to use. | Ich habe Clip {numbers} weggelassen, weil er zu kurz ist, um ihn zu verwenden. | {clips_tr} kullanılamayacak kadar kısa olduğu için çıkardım. |  |
| I left out clip {which}. | Ich habe Clip {which} weggelassen. | {which}. klibi dışarıda bıraktım. |  |
| I left out clips {join_unseen}: I can't tell what they show. Tell me and I'll add them. | Die Clips {join_unseen} habe ich weggelassen: Ich erkenne nicht, was sie zeigen. Sag es mir, dann ergänze ich sie. | Klip {join_unseen} dışarıda kaldı: ne gösterdiğini anlayamadım. Söylersen ekleyeyim. |  |
| I left out clips {numbers} because they are too short to use. | Ich habe die Clips {numbers} weggelassen, weil sie zu kurz sind, um sie zu verwenden. | {clips_tr} kullanılamayacak kadar kısa oldukları için çıkardım. |  |
| I left out clips {which}. | Ich habe die Clips {which} weggelassen. | {which}. klipleri dışarıda bıraktım. |  |
| I left out on-screen text that wasn't in your words. | Ich habe Text auf dem Bildschirm weggelassen, der nicht in deinen Worten vorkam. | Senin sözlerinde olmayan ekran yazısını eklemedim. |  |
| I left out the word I couldn't make out | Ich habe das Wort weggelassen, das ich nicht verstanden habe | Anlayamadığım kelimeyi çıkardım |  |
| I left out {omitted} based on your clip answer. | Ich habe {omitted} wegen deiner Clip-Antwort weggelassen. | Klip yanıtına göre {omitted} bölümünü çıkardım. |  |
| I left the title off because you didn't give me its words. Tell me what it should say and I'll add it | Ich habe den Titel weggelassen, weil du mir keinen Text dafür gegeben hast. Sag mir, was er sagen soll, dann füge ich ihn hinzu | Başlığı koymadım çünkü kelimelerini vermedin. Ne yazmasını istediğini söyle, ekleyeyim |  |
| I left those clips out. | Ich habe diese Clips weggelassen. | O klipleri dışarıda bıraktım. |  |
| I left {phrase} alone. | Ich habe {phrase} in Ruhe gelassen. | {phrase_capitalize} bölümüne dokunmadım. |  |
| I looked inside your clips and found every shot you named: {found}. Should I build the edit with them? | Ich habe in deine Clips hineingeschaut und jede Aufnahme gefunden, die du genannt hast: {found}. Soll ich den Edit damit erstellen? | Kliplerinin içine baktım ve istediğin her çekimi buldum: {found}. Videoyu bunlarla oluşturayım mı? |  |
| I made it long enough for every photo to play the length you gave it. | Ich habe es lang genug gemacht, damit jedes Foto so lange läuft, wie du es festgelegt hast. | Her fotoğraf verdiğin süre kadar kalsın diye videoyu uzattım. |  |
| I made it {possible:g} seconds instead of {wanted:g}. | Ich habe es {possible:g} Sekunden lang gemacht statt {wanted:g}. | {wanted:g} saniye yerine {possible:g} saniyelik yaptım. |  |
| I made that change in the editor. Part of your request needs a fresh edit, not an in-place tweak: tell me to redo it and I'll re-plan around your full request. | Ich habe diese Änderung im Editor gemacht. Ein Teil deiner Anfrage braucht einen neuen Edit statt einer direkten Anpassung: Sag mir, dass ich es neu machen soll, dann plane ich alles passend zu deiner ganzen Anfrage neu. | Bu değişikliği editörde yaptım. İsteğinin bir kısmı yerinde bir ayar değil, yeni bir düzenleme gerektiriyor: baştan yapmamı söylersen tüm isteğine göre planı yeniden kurarım. |  |
| I marked the {label} on a frame. | Ich habe {label} auf einem Bild markiert. | {label} bir karede işaretledim. |  |
| I matched {by_lyrics} take by your singing. | Ich habe {by_lyrics} Take anhand deines Gesangs zugeordnet. | {by_lyrics} çekimi söylediğin sözlere göre eşleştirdim. |  |
| I matched {by_lyrics} takes by your singing. | Ich habe {by_lyrics} Takes anhand deines Gesangs zugeordnet. | {by_lyrics} çekimi söylediğin sözlere göre eşleştirdim. |  |
| I matched {clip} to your sentences by their words, the meaning check was unavailable | Ich habe {clip} über die Wörter deinen Sätzen zugeordnet, die Prüfung nach Sinn war nicht verfügbar | {clip} klibini cümlelerinle kelimelerinden eşledim, anlam kontrolü çalışmadı |  |
| I need a saved request and its source evidence before offering this render. | Ich brauche eine gespeicherte Anfrage und die zugehörigen Belege, bevor ich dieses Video anbieten kann. | Bu oluşturmayı sunmadan önce kayıtlı istek ve kaynak kanıtları gerekiyor. |  |
| I need capture times for every selected clip to verify chronological order. | Um die chronologische Reihenfolge zu prüfen, brauche ich die Aufnahmezeit jedes ausgewählten Clips. | Çekim sırasını doğrulamak için seçilen her klibin çekim zamanını bilmem gerekiyor. |  |
| I need one concrete creative choice before I can make a useful edit decision. Which moment should viewers remember? | Ich brauche eine konkrete kreative Entscheidung von dir, bevor ich sinnvoll weiterschneiden kann. Welchen Moment sollen sich die Zuschauer merken? | Anlamlı bir düzenleme kararı verebilmem için net bir yaratıcı seçime ihtiyacım var. İzleyenler hangi anı hatırlamalı? |  |
| I need one detail before changing the draft. | Ich brauche noch ein Detail, bevor ich den Entwurf ändere. | Taslağı değiştirmeden önce bir ayrıntıya ihtiyacım var. |  |
| I need the selected idea's unchanged execution evidence before approval. | Ich brauche die unveränderten Umsetzungsbelege der ausgewählten Idee, bevor du sie freigibst. | Onaydan önce seçilen fikrin değişmemiş düzen kanıtına ihtiyacım var. |  |
| I need to clarify the requested changes before offering this render. Your saved edit is unchanged. | Ich muss die gewünschten Änderungen klären, bevor ich dieses Video anbiete. Dein gespeicherter Edit ist unverändert. | Bu oluşturmayı sunmadan önce istenen değişiklikleri netleştirmem gerekiyor. Kayıtlı düzenlemen değişmedi. |  |
| I need to establish the requested changes before offering approval. | Ich muss die gewünschten Änderungen klären, bevor ich die Freigabe anbiete. | Onay sunmadan önce istenen değişiklikleri belirlemem gerekiyor. |  |
| I need to know which clips are in the edit to verify their order. | Um ihre Reihenfolge zu prüfen, muss ich wissen, welche Clips im Edit sind. | Sıralarını doğrulamak için düzenlemede hangi kliplerin olduğunu bilmem gerekiyor. |  |
| I needed one more detail to do it | Dafür brauchte ich noch ein Detail | bunu yapmak için bir bilgiye daha ihtiyacım vardı |  |
| I never heard {names_join_or_unheard}, so those recordings never show. | Ich habe {names_join_or_unheard} nie gehört, deshalb erscheinen diese Aufnahmen nie. | {names_join_or_unheard} sözlerini hiç duymadım, o yüzden o kayıtlar hiç görünmüyor. |  |
| I never heard {names} in the take | Ich habe {names} im Take nie gehört | Videoda {names} sözünü duymadım |  |
| I never heard {names} in your voice | Ich habe {names} in deiner Stimme nie gehört | Sesinde {names} sözünü duymadım |  |
| I never heard {unheard_0}, so that recording never shows. | Ich habe {unheard_0} nie gehört, deshalb erscheint diese Aufnahme nie. | {unheard_0} sözünü hiç duymadım, o yüzden o kayıt hiç görünmüyor. |  |
| I prepared a simpler draft. | Ich habe einen einfacheren Entwurf vorbereitet. | Daha sade bir taslak hazırladım. |  |
| I prepared a {pacing} {edit_format} draft around the available footage. | Ich habe aus deinem vorhandenen Material einen Entwurf vorbereitet (Tempo: {pacing}, Format: {edit_format}). | Elindeki çekimlere göre bir taslak hazırladım. |  |
| I prepared the supported changes.· | Ich habe die unterstützten Änderungen vorbereitet.· | Desteklenen değişiklikleri hazırladım.· |  |
| I prepared this edit for the editor to validate and stage. | Ich habe diesen Edit vorbereitet, damit der Editor ihn prüft und übernimmt. | Bu düzenlemeyi hazırladım, editör kontrol edip uygulayacak. |  |
| I prepared {count} reversible editor change. | Ich habe {count} Änderung im Editor vorbereitet, die du rückgängig machen kannst. | Editörde {count} geri alınabilir değişiklik hazırladım. |  |
| I prepared {count} reversible editor changes. | Ich habe {count} Änderungen im Editor vorbereitet, die du rückgängig machen kannst. | Editörde {count} geri alınabilir değişiklik hazırladım. |  |
| I put {phrase} back to how it was. | Ich habe {phrase} wieder so gemacht, wie es war. | {phrase_capitalize} önceki hâline döndürüldü. |  |
| I restored the previous draft. Nothing was rendered. | Ich habe den vorherigen Entwurf wiederhergestellt. Es wurde kein Video erstellt. | Önceki taslağı geri getirdim. Yeni bir video oluşturulmadı. |  |
| I saved that direction and will apply it when this cut is ready. | Ich habe diese Richtung gespeichert und wende sie an, sobald dieser Schnitt fertig ist. | Bu yönlendirmeyi kaydettim, bu kurgu hazır olunca uygulayacağım. |  |
| I shaped a focused draft around the strongest available footage. | Ich habe einen fokussierten Entwurf um dein stärkstes Material herum gebaut. | Elindeki en güçlü çekimlerle odaklı bir taslak hazırladım. |  |
| I showed this one, but couldn't find the rest of your list in the take | Ich habe diesen gezeigt, konnte aber den Rest deiner Liste im Take nicht finden | Bunu gösterdim ama listenin geri kalanını çekiminde bulamadım |  |
| I showed this one, but couldn't show {missing} more from your list{tail} | Ich habe diesen gezeigt, konnte aber {missing} weitere aus deiner Liste nicht zeigen{tail} | Bunu gösterdim ama listenden {missing} maddeyi daha gösteremedim{tail} |  |
| I showed you where each of my moments is. | Ich habe dir gezeigt, wo jeder meiner Momente ist. | Anlarımın her birinin nerede olduğunu gösterdim. |  |
| I skipped your clip question, so pick those clips for me. | Ich habe deine Clip-Frage übersprungen, such du die Clips also für mich aus. | Klip sorusunu boş bıraktım, o klipleri sen belirle. |  |
| I started your image. It shows up on the card when it is ready, and you review it before it goes in your video. | Ich habe dein Bild gestartet. Es erscheint auf der Karte, sobald es fertig ist, und du prüfst es, bevor es ins Video kommt. | Görselini hazırlamaya başladım. Hazır olunca kartta görünür, videoya girmeden önce sen gözden geçirirsin. |  |
| I still need to know which part of {name} to leave out. | Ich muss noch wissen, welchen Teil von {name} ich weglassen soll. | {name} içinde hangi bölümü göstermeyeceğimi hâlâ bilmem gerekiyor. |  |
| I tapped the clips you asked about. | Ich habe die Clips angetippt, nach denen du gefragt hast. | Sorduğun klipleri seçtim. |  |
| I ticked the clips I found for {shown}{more}. Tap any others that show it too, or untick one that doesn't. | Ich habe die Clips angehakt, die ich für {shown}{more} gefunden habe. Tipp auf weitere, die es auch zeigen, oder nimm bei einem den Haken raus, der es nicht zeigt. | {shown}{more} için bulduğum klipleri işaretledim. Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır. |  |
| I ticked the first clip you filmed for "{category_get}". | Ich habe für "{category_get}" den ersten Clip angehakt, den du gefilmt hast. | "{category_get}" için çektiğin ilk klibi işaretledim. |  |
| I ticked the last clip you filmed for "{category_get}". | Ich habe für "{category_get}" den letzten Clip angehakt, den du gefilmt hast. | "{category_get}" için çektiğin son klibi işaretledim. |  |
| I told you which part of the song I filmed. | Ich habe dir gesagt, zu welchem Teil des Songs ich gefilmt habe. | Şarkının hangi kısmını çektiğimi söyledim. |  |
| I translated the {language_name_translated} speech into {rendered_name} captions. | Ich habe die {language_name_translated} Sprache in {rendered_name} Untertitel übersetzt. | {language_name_translated} konuşmayı {rendered_name} altyazıya çevirdim. |  |
| I turned off {label} | Ich habe {label} ausgeschaltet | {v1}{v2} kapattım |  |
| I understood {kept} of your clip instructions | Ich habe {kept} deiner Clip-Anweisungen verstanden | Klip talimatlarından {kept} tanesini anladım |  |
| I undid my last change ({what}). Save when you're happy with it. | Ich habe meine letzte Änderung rückgängig gemacht ({what}). Speichere, wenn du zufrieden bist. | Son değişikliğimi geri aldım ({what}). Memnun kaldığında kaydet. |  |
| I used a plain default title because none was given. | Ich habe einen schlichten Standardtitel verwendet, weil keiner angegeben war. | Başlık vermediğin için sade bir varsayılan başlık kullandım. |  |
| I used all {len_used} clips, including the {len_short} short one | Ich habe alle {len_used} Clips verwendet, auch {len_short} kurzen Clip | {len_used} klibin hepsini kullandım, {len_short} kısa klip de dahil |  |
| I used all {len_used} clips, including the {len_short} short ones | Ich habe alle {len_used} Clips verwendet, auch die {len_short} kurzen | {len_used} klibin hepsini kullandım, {len_short} kısa klip de dahil |  |
| I used {len_used} clips, including the {len_short} short one | Ich habe {len_used} Clips verwendet, auch {len_short} kurzen Clip | {len_used} klibin tamamını kullandım, {len_short} kısa klip de dahil |  |
| I used {len_used} clips, including the {len_short} short ones | Ich habe {len_used} Clips verwendet, auch die {len_short} kurzen | {len_used} klibin tamamını kullandım, {len_short} kısa klip de dahil |  |
| I will make this edit from the clips that are still attached. | Ich erstelle diesen Edit aus den Clips, die noch angehängt sind. | Bu düzenlemeyi hâlâ bağlı olan kliplerle yapacağım. |  |
| I will wait for your updated footage before making this edit. | Ich warte auf dein aktualisiertes Material, bevor ich diesen Edit erstelle. | Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim. |  |
| I won't promise or check a particular order; the clips still play in a sensible sequence. | Ich verspreche oder prüfe keine bestimmte Reihenfolge; die Clips laufen trotzdem in einer sinnvollen Abfolge. | Belirli bir sıra vaat etmem ya da kontrol etmem; klipler yine de mantıklı bir sırayla oynar. |  |
| I wrote the word you gave me | Ich habe das Wort geschrieben, das du mir gegeben hast | Verdiğin kelimeyi yazdım |  |
| I wrote these from what I saw in each clip, so tell me if any is wrong:· | Das habe ich aus dem geschrieben, was ich in jedem Clip gesehen habe, sag mir also, wenn etwas nicht stimmt:· | Bunları her klipte gördüklerime göre yazdım, yanlış olan varsa söyle:· |  |
| I'll check the framing on the finished video. | Ich prüfe den Bildausschnitt am fertigen Video. | Kadrajı video hazır olunca kontrol edeceğim. |  |
| I'll check the full video length, including its ending, after export. | Ich prüfe die volle Videolänge samt Ende nach dem Export. | Kapanış dahil tam video süresini dışa aktardıktan sonra kontrol edeceğim. |  |
| I'll check where the video starts and ends on the finished video. | Ich prüfe am fertigen Video, wo es beginnt und endet. | Videonun nerede başlayıp bittiğini video hazır olunca kontrol edeceğim. |  |
| I'll check where the video starts on the finished video. | Ich prüfe am fertigen Video, wo es beginnt. | Videonun nereden başladığını video hazır olunca kontrol edeceğim. |  |
| I'll crop the sideways clips to fill the vertical frame, keeping faces in it, and check it on the finished video | Ich schneide die Querformat-Clips so zu, dass sie das Hochformat füllen, halte die Gesichter im Bild und prüfe es am fertigen Video | Yatay klipleri dikey ekranı dolduracak şekilde kırpacağım, yüzleri kadrajda tutacağım ve bitmiş videoda kontrol edeceğim |  |
| I'll cut the line you named out of your take if I find it said once, and tell you after | Ich schneide die Zeile, die du genannt hast, aus deinem Take, wenn ich sie genau einmal finde, und sage dir danach Bescheid | Söylediğin cümleyi çekiminde bir kez geçiyorsa keseceğim ve sonra sana söyleyeceğim |  |
| I'll cut this as a fast montage instead of a guided story: {v1} | Ich schneide das als schnelle Montage statt als geführte Story: {v1} | Bunu {label_tr} yerine hızlı montaj olarak keseceğim: {v2} |  |
| I'll cut this as a fast montage instead of a text explainer: {v1} | Ich schneide das als schnelle Montage statt als Text-Erklärvideo: {v1} | Bunu {label_tr} yerine hızlı montaj olarak keseceğim: {v2} |  |
| I'll cut this as a fast montage instead of a {label_en}: {v1} | Ich schneide das als schnelle Montage statt als {label_en}: {v1} | Bunu {label_tr} yerine hızlı montaj olarak keseceğim: {v2} |  |
| I'll draw a curly arrow next to your ending text | Ich zeichne einen geschwungenen Pfeil neben deinen Schlusstext | Kapanış yazının yanına kıvrımlı bir ok çizeceğim |  |
| I'll end the video on that colour screen with your text, after your last word | Ich beende das Video nach deinem letzten Wort mit diesem Farbbild und deinem Text | Son sözünden sonra videoyu o renkli ekranla ve yazınla bitireceğim |  |
| I'll keep the sideways clips whole with black bars and check it on the finished video | Ich lasse die Querformat-Clips ganz mit schwarzen Balken und prüfe es am fertigen Video | Yatay klipleri siyah bantlarla bütün bırakacağım ve bitmiş videoda kontrol edeceğim |  |
| I'll keep your take· | Ich behalte deinen Take· | Çekiminin· |  |
| I'll keep your take from {at_start} on, if I find that line; if not, it opens at the start | Ich behalte deinen Take ab {at_start}, wenn ich diesen Satz finde; sonst beginnt das Video am Anfang | {v1}{at_start} cümlesinden sonrasını korurum, o cümleyi bulursam; bulamazsam video baştan açılır |  |
| I'll keep your take from {at_start} to right after {at_end}, if I find those lines | Ich behalte deinen Take von {at_start} bis kurz nach {at_end}, wenn ich diese Sätze finde | {v1}{at_start} cümlesinden {at_end} cümlesinin hemen sonrasına kadar olan kısmını korurum, bu cümleleri bulursam |  |
| I'll keep your take up to right after {at_end}, if I find that line; if not, it runs to the end | Ich behalte deinen Take bis kurz nach {at_end}, wenn ich diesen Satz finde; sonst läuft das Video bis zum Ende | {v1}{at_end} cümlesinin hemen sonrasına kadar olan kısmını korurum, o cümleyi bulursam; bulamazsam video sonuna kadar sürer |  |
| I'll leave out the word I couldn't make out | Ich lasse das Wort weg, das ich nicht verstanden habe | Anlayamadığım kelimeyi çıkarırım |  |
| I'll place that text on the matching words in your recording. If they repeat, I'll need the occurrence you mean. | Ich setze den Text auf die passenden Wörter in deiner Aufnahme. Wenn sie sich wiederholen, muss ich wissen, welches Mal du meinst. | Yazıyı kaydındaki eşleşen sözlerde göstereceğim. Sözler tekrarlanırsa hangi tekrarı istediğini bilmem gerekecek. |  |
| I'll put it above your title when the video is made; if there's no room without covering your face or captions, I'll tell you | Ich setze es über deinen Titel, wenn das Video erstellt wird; wenn kein Platz ist, ohne dein Gesicht oder die Untertitel zu verdecken, sage ich es dir | Video yapılınca onu başlığının üstüne koyacağım; yüzünü ya da altyazılarını kapatmadan yer yoksa sana söyleyeceğim |  |
| I'll show it on the· | Ich zeige ihn bei· | Yazıyı seçtiğin· |  |
| I'll show it on the {word} you picked, at {clock} in your take; if I can't find that one, it won't appear | Ich zeige ihn bei dem von dir gewählten {word}, bei {clock} in deinem Take; finde ich diese Stelle nicht, erscheint er nicht | {v1}{word} anında, çekiminin {clock} noktasında gösteririm; o anı bulamazsam yazı çıkmaz |  |
| I'll show it the first time that word is said in your take; I can't tell who's speaking, so it may appear when someone else says it | Ich zeige ihn, wenn das Wort in deinem Take zum ersten Mal gesagt wird; ich erkenne nicht, wer spricht, deshalb kann er auch erscheinen, wenn jemand anderes es sagt | Yazıyı çekiminde o kelime ilk söylendiğinde gösteririm; kimin konuştuğunu ayırt edemiyorum, o yüzden başka biri söylediğinde çıkabilir |  |
| I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear | Ich zeige ihn in dem Moment, in dem du das Wort in deinem Take sagst; wenn ich das Wort nicht höre, erscheint er nicht | Çekiminde o kelimeyi söylediğin anda yazıyı gösteririm; kelimeyi duymazsam yazı çıkmaz |  |
| I'll show it when I hear those words in your voiceover | Ich zeige es, sobald ich diese Worte in deinem Voiceover höre | Seslendirmende o sözleri duyduğumda gösteririm |  |
| I'll show it when that part of your talk starts; if I don't hear where it starts, it won't appear | Ich zeige ihn, wenn dieser Teil deiner Rede beginnt; wenn ich nicht höre, wo er beginnt, erscheint er nicht | Konuşmanda o bölüm başladığında gösteririm; nerede başladığını duymazsam çıkmaz |  |
| I'll show these words one at a time when I prepare the video, then check. | Ich zeige diese Wörter nacheinander, wenn ich das Video vorbereite, und prüfe es danach. | Bu kelimeleri videoyu hazırlarken tek tek göstereceğim, sonra kontrol ederim. |  |
| I'll show your Visuals as cards when you talk about them; one you never talk about won't appear | Ich zeige deine Visuals als Karten, wenn du darüber sprichst; eines, über das du nie sprichst, erscheint nicht | Görsellerini onlardan bahsettiğin anda kart olarak gösteririm; hiç bahsetmediğin görsel çıkmaz |  |
| I'll spell {and_names_names} the way you wrote it and check it in the finished video | ich schreibe {and_names_names} so, wie du es geschrieben hast, und prüfe es im fertigen Video | şunu senin yazdığın gibi yazıp bitmiş videoda kontrol edeceğim: {and_names_names} |  |
| I'll spell {and_names_names} the way you wrote them and check them in the finished video | ich schreibe {and_names_names} so, wie du sie geschrieben hast, und prüfe sie im fertigen Video | şunları senin yazdığın gibi yazıp bitmiş videoda kontrol edeceğim: {and_names_names} |  |
| I'll start the video at that line if I find it in your take; if not, it opens at the start | Ich starte das Video an dieser Zeile, wenn ich sie in deinem Take finde; wenn nicht, beginnt es am Anfang | Videoyu o cümlenin başladığı yerden başlatırım; çekiminde bulamazsam baştan açılır |  |
| I'll use your song as the background music for this edit. | Ich nutze deinen Song als Hintergrundmusik für diesen Edit. | Şarkını bu düzenlemede arka plan müziği olarak kullanacağım. |  |
| I'll write the word you gave me | Ich schreibe das Wort, das du mir gegeben hast | Verdiğin kelimeyi yazarım |  |
| I'll zoom in on the text when it's made; I can't confirm it before then. | Ich zoome auf den Text, wenn das Video erstellt wird; vorher kann ich das nicht bestätigen. | Video yapılınca yazılara yakınlaşacağım; öncesinde doğrulayamıyorum. |  |
| I'll zoom onto the text when the video is made; I can't confirm it before then | Ich zoome auf den Text, wenn das Video erstellt wird; vorher kann ich das nicht bestätigen | Video yapılınca yazılara yakınlaşacağım; öncesinde doğrulayamıyorum |  |
| I'm checking sources for the facts you asked me to source, so you can see the exact sourced wording before you approve. If this takes too long, send your message again, or ask for a version without sources. | Ich prüfe Quellen für die Fakten, bei denen du Quellen wolltest, damit du den genauen Wortlaut mit Quellen siehst, bevor du freigibst. Wenn das zu lange dauert, schick deine Nachricht nochmal oder bitte um eine Version ohne Quellen. | Onaylamadan önce kaynaklı metni görebilmen için kaynak istediğin bilgileri kontrol ediyorum. Uzun sürerse mesajını tekrar gönder ya da kaynaksız bir sürüm iste. |  |
| I'm cutting it every time it's said. | Ich schneide es jedes Mal heraus, wenn es gesagt wird. | Her söylendiğinde kesiyorum. |  |
| I'm freezing that moment on its middle frame. | Ich friere diesen Moment auf seinem mittleren Bild ein. | O anı, ortasındaki karede donduruyorum. |  |
| I'm freezing the frame at {at}s into the clip. | Ich friere das Bild bei {at} s im Clip ein. | Kareyi klibin {at}. saniyesinde donduruyorum. |  |
| I'm leaving that word out of the captions. | Ich lasse dieses Wort in den Untertiteln weg. | O kelimeyi altyazıdan çıkarıyorum. |  |
| I'm leaving the freeze out, as you chose. | Ich lasse das Standbild weg, wie du es gewählt hast. | Seçtiğin gibi donmayı kaldırıyorum. |  |
| I'm leaving the title off, since you didn't give me the words. | Ich lasse den Titel weg, weil du mir keinen Text gegeben hast. | Kelimeleri söylemediğin için başlığı koymuyorum. |  |
| I'm making it {seconds} seconds. | Ich mache es {seconds} Sekunden lang. | Videoyu {seconds} saniye yapıyorum. |  |
| I'm not promising a particular order. | Ich verspreche keine bestimmte Reihenfolge. | Belirli bir sıra vaat etmiyorum. |  |
| I'm not sure where one of your clips sits in the song. They're in the order I think; drag any that are out of place. | Ich bin nicht sicher, wo einer deiner Clips im Song liegt. Sie sind in der Reihenfolge, die ich für richtig halte; zieh die, die falsch liegen, an die richtige Stelle. | Kliplerinden birinin şarkıda nereye geldiğinden emin değilim. Klipleri doğru olduğunu düşündüğüm sırayla dizdim; yeri yanlış olanları sürükle. |  |
| I'm not sure where {unsure} of your clips sit in the song. They're in the order I think; drag any that are out of place. | Ich bin nicht sicher, wo {unsure} deiner Clips im Song liegen. Sie sind in der Reihenfolge, die ich für richtig halte; zieh die, die falsch liegen, an die richtige Stelle. | Kliplerinden {unsure} tanesinin şarkıda nereye geldiğinden emin değilim. Klipleri doğru olduğunu düşündüğüm sırayla dizdim; yeri yanlış olanları sürükle. |  |
| I'm not using the planned title animation ({words_value}): the title follows your own animation ask | Ich verwende die geplante Titel-Animation ({words_value}) nicht: Der Titel richtet sich nach deinem eigenen Animationswunsch | Planlanan başlık animasyonunu ({words_value}) kullanmıyorum: başlık senin animasyon isteğini izliyor |  |
| I'm skipping that one, as you chose. | Ich überspringe diesen, wie du es gewählt hast. | Seçtiğin gibi onu atlıyorum. |  |
| I'm still analysing your song and matching your clips to it. Send your message again in a moment and I'll pick up where I left off. | Ich analysiere noch deinen Song und ordne deine Clips dazu zu. Schick deine Nachricht gleich nochmal, dann mache ich da weiter, wo ich aufgehört habe. | Şarkını hâlâ analiz ediyorum ve kliplerini ona göre eşleştiriyorum. Birazdan mesajını tekrar gönder, kaldığım yerden sürdüreyim. |  |
| I'm still checking some of your clips against that request. Reply "go ahead" in a moment and I'll pick up where I left off. No need to send the whole request again. | Ich prüfe noch einige deiner Clips für diese Anfrage. Antworte gleich mit "weiter", dann mache ich da weiter, wo ich aufgehört habe. Du musst nicht die ganze Anfrage nochmal schicken. | İsteğine göre bazı kliplerini hâlâ kontrol ediyorum. Birazdan "devam" yaz, kaldığım yerden sürdüreyim. Tüm isteği tekrar göndermene gerek yok. |  |
| I'm still checking some of your clips. I'll carry on with your new message. | Ich prüfe noch einige deiner Clips. Ich mache mit deiner neuen Nachricht weiter. | Kliplerinden bazılarını hâlâ kontrol ediyorum. Yeni mesajınla devam edeceğim. |  |
| I'm still checking {count} of your clips. Your request and completed answers are saved. Ask me to continue once those clips are ready. | Ich prüfe noch {count} deiner Clips. Deine Anfrage und die schon gegebenen Antworten sind gespeichert. Bitte mich weiterzumachen, sobald diese Clips bereit sind. | Kliplerinden {count} tanesini hâlâ kontrol ediyorum. İsteğin ve tamamlanan yanıtlar kaydedildi. Bu klipler hazır olunca devam etmemi iste. |  |
| I'm still stuck on the same thing. Tell me what to change or leave out, and I'll try again. | Ich hänge immer noch an derselben Stelle. Sag mir, was ich ändern oder weglassen soll, dann versuche ich es nochmal. | Hâlâ aynı yere takıldım. Neyi değiştireyim ya da neyi çıkarayım söyle, yeniden deneyeyim. |  |
| I'm using the one at {clock_pin_approx_s} in your take. | Ich nehme die Stelle bei {clock_pin_approx_s} in deinem Take. | Çekimindeki {clock_pin_approx_s} anındakini kullanıyorum. |  |
| I'm using the order you added the clips. | Ich nehme die Reihenfolge, in der du die Clips hinzugefügt hast. | Klipleri eklediğin sırayla kullanıyorum. |  |
| If Clean up speech is offered when you approve, choose it and every pause is cut down to about 0.3 s | Wenn beim Freigeben „Clean up speech“ angeboten wird, wähl es aus, dann wird jede Pause auf etwa 0,3 s gekürzt | Onaylarken “Konuşmayı temizle” çıkarsa onu seç, her duraklama yaklaşık 0,3 sn'ye kadar kısalır |  |
| If Clean up speech is offered when you approve, choose it to cut the long pauses | Wenn beim Freigeben „Clean up speech“ angeboten wird, wähl es aus, um die langen Pausen herauszuschneiden | Onaylarken “Konuşmayı temizle” çıkarsa, uzun duraklamaları kesmek için onu seç |  |
| If that clip plays as a cutaway over your voice, it plays without its own sound; I can't tell yet which clip you talk in, and that clip keeps its sound | Wenn dieser Clip als Zwischenschnitt über deiner Stimme läuft, spielt er ohne eigenen Ton; ich weiß noch nicht, in welchem Clip du sprichst, und dieser Clip behält seinen Ton | O klip sesinin üzerinde gösterilirse kendi sesi olmadan oynar; hangi klipte konuştuğunu henüz bilmiyorum, konuştuğun klibin sesi kalır |  |
| Image generation is paused right now, so I didn't make one. Nothing in the edit changed. | Die Bilderstellung ist gerade pausiert, deshalb habe ich keins erstellt. Am Edit hat sich nichts geändert. | Görsel üretimi şu an durduruldu, bu yüzden görsel yapmadım. Düzenlemede hiçbir şey değişmedi. |  |
| Importing from Drive didn't finish. Try again. | Der Import aus Drive wurde nicht fertig. Versuch es nochmal. | Drive'dan içe aktarma tamamlanmadı. Tekrar dene. |  |
| In a Talking edit the clips shown over your voice play without their own sound.· | In einem Talking-Edit laufen die Clips, die über deiner Stimme gezeigt werden, ohne ihren eigenen Ton.· | Konuşmalı düzenlemede sesinin üzerinde gösterilen klipler kendi sesi olmadan oynar.· |  |
| In a Talking edit, clip sound can be turned on for a whole cutaway clip, not for seconds, a spoken line or the first or last clip | In einem Talking-Edit kann der Clip-Ton nur für einen ganzen Zwischenschnitt-Clip eingeschaltet werden, nicht für einzelne Sekunden, eine gesprochene Zeile oder den ersten oder letzten Clip | Konuşmalı düzenlemede klip sesi, saniyeler, söylenen bir cümle ya da ilk veya son klip için değil, yalnızca tam bir araya giren klip için açılabilir |  |
| In a voiceover edit, photos from Visuals show up on a word you say, so I left out the ones that aren't tied to a word. | In einem Voiceover-Edit erscheinen Fotos aus Visuals bei einem Wort, das du sagst, deshalb habe ich die weggelassen, die an kein Wort gebunden sind. | Seslendirmeli düzenlemede Visuals'taki fotoğraflar söylediğin bir kelimede beliriyor, o yüzden bir kelimeye bağlı olmayanları koymadım. |  |
| It changed other things, not the one you named: {phrase} | Es hat andere Dinge geändert, nicht das, das du genannt hast: {phrase} | Söylediğini değil, başka şeyleri değiştirdi: {phrase} |  |
| It doesn't come back to you after the other footage | Es geht nach dem anderen Material nicht zu dir zurück | Diğer çekimlerden sonra sana geri dönmüyor |  |
| It is the only clip in this edit | Es ist der einzige Clip in diesem Edit | Bu düzenlemedeki tek klip bu |  |
| It looks only for pauses and filler sounds, so a sentence you restarted stays in. | Es sucht nur nach Pausen und Fülllauten, deshalb bleibt ein Satz, den du neu angesetzt hast, drin. | Yalnızca duraklamaları ve dolgu seslerini arar; baştan başladığın bir cümle videoda kalır. |  |
| It never cuts to you talking | Es schneidet nie zu dir beim Sprechen | Hiçbir yerde konuşurken sana geçmiyor |  |
| It would sit too close to the previous clip you asked to play | Es läge zu nah an dem vorherigen Clip, den du abspielen wolltest | İstediğin önceki klibe çok yakın olurdu |  |
| Italian | Italienisch |  |  |
| I’d try “{candidate}”. Does this wording work? | Ich würde „{candidate}“ probieren. Passt dir dieser Wortlaut? | Şunu deneyebilirim: “{candidate}”. Bu ifade sana uyar mı? |  |
| I’m working on the edit plan. Your project is saved. | Ich arbeite am Edit-Plan. Dein Projekt ist gespeichert. | Düzenleme planı üzerinde çalışıyorum. Projen kaydedildi. |  |
| I’ve refreshed your project status. | Ich habe den Status deines Projekts aktualisiert. | Projenin durumunu güncelledim. |  |
| I’ve saved your request. I’m analyzing your clips and will continue automatically. | Ich habe deine Anfrage gespeichert. Ich analysiere deine Clips und mache automatisch weiter. | İsteğini kaydettim. Kliplerini inceliyorum, bitince kendiliğinden devam edeceğim. |  |
| Keep about {clean_seconds_new} seconds (the part your lines keep, instead of {clean_seconds_old}) | Etwa {clean_seconds_new} Sekunden behalten (der Teil, den deine Zeilen behalten, statt {clean_seconds_old}) | Yaklaşık {clean_seconds_new} saniye olsun ({clean_seconds_old} saniye yerine, cümlelerinin tuttuğu kısım) |  |
| Keep it | So lassen | Böyle kalsın |  |
| Keep it strictly chronological; groups may interleave | Streng chronologisch lassen; die Gruppen dürfen sich mischen | Tam kronolojik kalsın; {tr_word} klipleri iç içe geçebilir |  |
| Keep it strictly chronological; sports may interleave | Streng chronologisch lassen; die Sportarten dürfen sich mischen | Tam kronolojik kalsın; {tr_word} klipleri iç içe geçebilir |  |
| Keep it strictly chronological; {word}s may interleave | Streng chronologisch lassen; die Gruppen nach „{word}“ dürfen sich mischen | Tam kronolojik kalsın; {tr_word} klipleri iç içe geçebilir |  |
| Keep the video at {exc_minimum_duration_s:g}s | Video bei {exc_minimum_duration_s:g} s lassen | Videoyu {exc_minimum_duration_s:g} saniye tut |  |
| Keep those {kept_s} seconds | Diese {kept_s} Sekunden behalten | O {kept_s} saniyeyi kullan |  |
| Keep {asked_s} seconds, the last {v1} seconds play without voice | {asked_s} Sekunden behalten, die letzten {v1} Sekunden laufen ohne Stimme | {asked_s} saniye kalsın, son {v1} saniye sessiz oynasın |  |
| Keep {asked} seconds with {kept} clips | {asked} Sekunden behalten, mit {kept} Clips | {asked} saniyeyi koru, {kept} klip kullan |  |
| Keep {asked} seconds, photos at the usual pace | {asked} Sekunden behalten, Fotos im normalen Tempo | {asked} saniyeyi koru, fotoğraflar normal hızda |  |
| Keep {clean_seconds_new} seconds (as long as your voice, shortened from {clean_seconds_old}) | {clean_seconds_new} Sekunden behalten (so lang wie deine Stimme, gekürzt von {clean_seconds_old}) | {clean_seconds_new} saniye olsun (sesin kadar, {clean_seconds_old} saniyeden kısaltıldı) |  |
| Keep {clean_seconds_new} seconds (extended from {clean_seconds_old} so every clip can be seen) | {clean_seconds_new} Sekunden behalten (verlängert von {clean_seconds_old}, damit jeder Clip zu sehen ist) | {clean_seconds_new} saniye olsun ({clean_seconds_old} saniyeden uzatıldı, böylece her klip görülebiliyor) |  |
| Keep {clean_seconds_new} seconds (so every timed photo fits) | {clean_seconds_new} Sekunden behalten (damit jedes Foto mit festgelegter Dauer passt) | {clean_seconds_new} saniye olsun (süre verilen her fotoğraf sığsın diye) |  |
| Keep {clean_seconds_new} seconds (the available source length, instead of {clean_seconds_old}) | {clean_seconds_new} Sekunden behalten (die verfügbare Länge des Materials, statt {clean_seconds_old}) | {clean_seconds_new} saniye olsun ({clean_seconds_old} saniye yerine, eldeki çekimlerin uzunluğu) |  |
| Kept black bars on the {clips_n}, so nothing is cropped | Schwarze Balken bei {clips_n} beibehalten, also ist nichts abgeschnitten | {clips_n} siyah bantlarla bütün kaldı, hiçbir şey kırpılmadı |  |
| Korean | Koreanisch |  |  |
| Kria is preparing the render from your confirmed direction. | Kria bereitet das Video nach deiner bestätigten Richtung vor. | Kria onayladığın yöne göre videoyu hazırlamaya başlıyor. |  |
| Kria is rendering your video ({phase}). I’ll update this chat when it’s ready. | Kria erstellt gerade dein Video (Schritt: {phase}). Ich melde mich in diesem Chat, sobald es fertig ist. | Kria videonu hazırlıyor. Hazır olunca bu sohbete yazacağım. |  |
| Kria is rendering your video. I’ll update this chat when it’s ready. | Kria erstellt dein Video. Ich melde mich in diesem Chat, sobald es fertig ist. | Kria videonu hazırlıyor. Hazır olunca bu sohbete yazacağım. |  |
| Kria is still working on the confirmed cut. I’ll update this chat when it’s ready. | Kria arbeitet noch am bestätigten Schnitt. Ich melde mich in diesem Chat, sobald er fertig ist. | Kria onayladığın kurgu üzerinde çalışmaya devam ediyor. Hazır olunca bu sohbete yazacağım. |  |
| Kria's AI is at its limit | Krias KI ist am Limit | Kria'nın yapay zekâsı sınırda |  |
| Kria's AI service has reached its usage limit for now, so your video wasn't made. Your edit and footage are saved. Trying again right away won't help; try again later. | Krias KI-Dienst hat sein Nutzungslimit vorerst erreicht, deshalb wurde dein Video nicht erstellt. Dein Edit und dein Material sind gespeichert. Sofort nochmal versuchen hilft nicht; versuch es später nochmal. | Kria'nın yapay zekâ hizmeti şimdilik kullanım sınırına ulaştı, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Hemen tekrar denemek işe yaramaz; daha sonra tekrar dene. |  |
| Labelled {count} clip. | {count} Clip beschriftet. | {count} klibe etiket ekledim. |  |
| Labelled {count} clips. | {count} Clips beschriftet. | {count} klibe etiket ekledim. |  |
| Leave it out | Weglassen | Çıkar |  |
| Leave those clips out | Diese Clips weglassen | O klipleri dışarıda bırak |  |
| Leaves out: {listed} | Lässt weg: {listed} | Çıkarılacak: {listed} |  |
| Left out: {names}. | Weggelassen: {names}. | Çıkardıklarım: {names}. |  |
| Let's finish choosing the wording before preparing the edit. | Lass uns erst den Wortlaut fertig auswählen, bevor ich den Edit vorbereite. | Düzenlemeyi hazırlamadan önce ekrandaki yazıyı netleştirelim. |  |
| Let's review the edit plan with your saved wording before changing this video. | Lass uns den Edit-Plan mit deinem gespeicherten Wortlaut durchgehen, bevor ich dieses Video ändere. | Bu videoyu değiştirmeden önce düzenleme planını kaydettiğin yazıyla birlikte gözden geçirelim. |  |
| Lined up {who} on the center | {who} in die Mitte gerückt | {who} ortaya hizalandı |  |
| Lined up {who} on the left | {who} nach links gerückt | {who} sola hizalandı |  |
| Lined up {who} on the right | {who} nach rechts gerückt | {who} sağa hizalandı |  |
| Lip-sync | Lip-Sync | Dudak senkronu |  |
| Lip-sync keeps the song where you filmed it. | Lip-Sync lässt den Song dort, wo du gefilmt hast. | Dudak senkronu şarkıyı çektiğin yerde tutar. |  |
| Longer: each clip stays on screen a little longer. | Länger: Jeder Clip bleibt etwas länger im Bild. | Daha uzun: her klip ekranda biraz daha uzun kalır. |  |
| Made {who} bigger ({old:g}→{new:g}px) | {who} vergrößert ({old:g}→{new:g}px) | {who} büyüttüm ({old:g}→{new:g}px) |  |
| Made {who} smaller ({old:g}→{new:g}px) | {who} verkleinert ({old:g}→{new:g}px) | {who} küçülttüm ({old:g}→{new:g}px) |  |
| Make it without these | Ohne diese erstellen | Bunlar olmadan hazırla |  |
| Make it {shown} seconds | {shown} Sekunden lang machen | Videoyu {shown} saniye yap |  |
| Mark the {label} on one frame so I can keep the text off it. Or tell me the text can go anywhere. | Markier {label} auf einem Bild, damit ich den Text davon fernhalten kann. Oder sag mir, dass der Text überall stehen darf. | Yazıyı uzak tutabilmem için {label} bir karede işaretle. Ya da yazının her yere gelebileceğini söyle. |  |
| Mark {named} on a frame so I can keep the text off it | Markier {named} auf einem Bild, damit ich den Text davon fernhalten kann | Yazıyı uzak tutabilmem için {named_tr} bir karede işaretle |  |
| Matching your footage to music didn't finish. Try again. | Die Abstimmung deines Materials auf Musik wurde nicht fertig. Versuch es nochmal. | Çekimlerini müzikle eşleştirme tamamlanmadı. Tekrar dene. |  |
| May not match in the video. Nothing changed. | Stimmt im Video vielleicht nicht überein. Nichts wurde geändert. | Videoda uyuşmuyor olabilir. Hiçbir şey değişmedi. |  |
| Montage format | Format: Montage | Format: Montaj |  |
| More than one clip says {quote}, so I couldn't tell which to keep whole | Mehr als ein Clip sagt {quote}, deshalb konnte ich nicht erkennen, welchen ich ganz lassen soll | {quote} birden fazla klipte geçiyor, hangisini bütün bırakacağımı anlayamadım |  |
| More than one of your clips has someone talking. | In mehr als einem deiner Clips spricht jemand. | Birden fazla klibinde biri konuşuyor. |  |
| More than one of your clips shows {shown}{more}. For each one, keep the clip to use ticked and untick the others. | Mehr als einer deiner Clips zeigt {shown}{more}. Lass bei jedem den Clip angehakt, der verwendet werden soll, und nimm bei den anderen den Haken raus. | Şunlar birden fazla klibinde görünüyor: {shown}{more}. Her biri için kullanılacak klibi işaretli bırak, diğerlerinin işaretini kaldır. |  |
| Moved the captions to the bottom | Untertitel nach unten verschoben | Altyazılar alt konuma taşındı |  |
| Moved the captions to the middle | Untertitel in die Mitte verschoben | Altyazılar orta konuma taşındı |  |
| Moved the captions to the top | Untertitel nach oben verschoben | Altyazılar üst konuma taşındı |  |
| Moved {who} | {who} verschoben | {who} taşındı |  |
| Moved {who} to the bottom | {who} nach unten verschoben | {who} alt konuma taşındı |  |
| Moved {who} to the middle | {who} in die Mitte verschoben | {who} orta konuma taşındı |  |
| Moved {who} to the top | {who} nach oben verschoben | {who} üst konuma taşındı |  |
| Moved {who} to the {named} | {who} nach {named} verschoben | {who} {spot_tr} konuma taşındı |  |
| Music under your voiceover isn't available on this iPhone yet, so the video plays your voice without music | Musik unter deinem Voiceover ist auf diesem iPhone noch nicht verfügbar, deshalb läuft das Video mit deiner Stimme ohne Musik | Seslendirmenin altına müzik bu iPhone'da henüz eklenemiyor, o yüzden video müziksiz, sadece sesinle oynayacak |  |
| Narrated Planned format | Format: Mit Voiceover | Format: Seslendirmeli |  |
| Narrated Ready format | Format: Mit Voiceover | Format: Seslendirmeli |  |
| Narrated format | Format: Mit Voiceover | Format: Seslendirmeli |  |
| Narrating across several clips isn't on iPhone yet. Record a voiceover, or keep one clip for talking-to-camera. | Erzählen über mehrere Clips gibt es auf dem iPhone noch nicht. Nimm ein Voiceover auf oder behalte einen Clip, in dem du in die Kamera sprichst. | Birden fazla klip üzerinde anlatım iPhone'da henüz yok. Bir seslendirme kaydet ya da kameraya konuşma için tek klip kullan. |  |
| Narration labels must be resolved against the recorded voiceover. | Die Erzähl-Labels müssen anhand des aufgenommenen Voiceovers zugeordnet werden. | Anlatım etiketleri kaydedilen seslendirmeye göre çözülmeli. |  |
| No clip got its own text in this draft. | In diesem Entwurf hat kein Clip einen eigenen Text bekommen. | Bu taslakta hiçbir klibe kendi yazısı gelmedi. |  |
| No clip's own sound plays under your voice | Kein Clip-Originalton läuft unter deiner Stimme | Hiçbir klibin kendi sesi seslendirmenin altında çalmıyor |  |
| No filming time for clip {join_untimed}. | Keine Aufnahmezeit für Clip {join_untimed}. | Klip {join_untimed} için çekim saati yok. |  |
| No filming time for clips {join_untimed}. | Keine Aufnahmezeit für die Clips {join_untimed}. | Klip {join_untimed} için çekim saati yok. |  |
| No freeze | Kein Standbild | Donma olmasın |  |
| No freeze, as you chose | Kein Standbild, wie du es gewählt hast | Seçtiğin gibi donma yok |  |
| No matching music was found for this edit. Try again, or pick a track yourself. | Für diesen Edit wurde keine passende Musik gefunden. Versuch es nochmal oder wähl selbst einen Track aus. | Bu düzenleme için uygun müzik bulunamadı. Tekrar dene ya da müziği kendin seç. |  |
| No music | Keine Musik | Müzik yok |  |
| No music, as asked. | Keine Musik, wie gewünscht. | İstediğin gibi müzik yok. |  |
| No pauses or filler words were found, so there's no cleanup choice to make. Approve again. | Es wurden keine Pausen oder Füllwörter gefunden, deshalb gibt es nichts zu bereinigen. Gib nochmal frei. | Duraklama ya da dolgu sözcüğü bulunmadı, yani yapılacak bir temizleme seçimi yok. Tekrar onayla. |  |
| No phone font can draw "{characters}". Remove it or change "{text}". | Keine Schriftart auf dem Handy kann "{characters}" darstellen. Entferne es oder ändere "{text}". | "{characters}" hiçbir telefon yazı tipiyle yazılamıyor. Onu çıkar ya da "{text}" metnini değiştir. |  |
| No pop-in for {names_missing} | Keine Einblendung für {names_missing} | Şunlar için çıkan görsel yok: {names_missing} |  |
| No recording for {names_join_none_for}, so the one before stays on. | Keine Aufnahme für {names_join_none_for}, deshalb bleibt die vorherige im Bild. | {names_join_none_for} için kayıt yok, o yüzden öncekisi ekranda kalıyor. |  |
| No title, as asked. | Kein Titel, wie gewünscht. | İstediğin gibi başlık yok. |  |
| No two clips in the edit come from the same video file | Keine zwei Clips im Edit stammen aus derselben Videodatei | Düzenlemedeki hiçbir klip aynı video dosyasından gelmiyor |  |
| No, something's wrong | Nein, etwas stimmt nicht | Hayır, bir şey yanlış |  |
| None of the pop-ins plays a sound | Keine der Einblendungen spielt einen Ton ab | Çıkan görsellerin hiçbiri ses çalmıyor |  |
| None of the pop-ins shows a photo or sticker | Keine der Einblendungen zeigt ein Foto oder einen Sticker | Çıkan görsellerin hiçbiri fotoğraf ya da çıkartma göstermiyor |  |
| None of the {total} clips got its own text in this draft. | Keiner der {total} Clips hat in diesem Entwurf einen eigenen Text bekommen. | Bu taslakta {total} klibin hiçbirine kendi yazısı gelmedi. |  |
| None of these — keep it from the start | Keins davon – von Anfang an behalten | Hiçbiri — baştan başlasın |  |
| None of these — keep it to the end | Keins davon – bis zum Ende behalten | Hiçbiri — sona kadar sürsün |  |
| None of what you say is about it, so I left it out | Nichts von dem, was du sagst, handelt davon, deshalb habe ich es weggelassen | Anlattığın hiçbir şey onunla ilgili değil, o yüzden dışarıda bıraktım |  |
| None of your Visuals showed as a card | Keines deiner Visuals wurde als Karte gezeigt | Görsellerinin hiçbiri kart olarak çıkmadı |  |
| None of your clips has sound, so I can't keep the camera audio. Want me to use music instead? | Keiner deiner Clips hat Ton, deshalb kann ich den Kameraton nicht behalten. Soll ich stattdessen Musik nehmen? | Kliplerinin hiçbirinde ses yok, o yüzden kamera sesini koruyamam. Onun yerine müzik kullanayım mı? |  |
| None of your clips has sound, so this edit doesn't use camera audio. | Keiner deiner Clips hat Ton, deshalb nutzt dieser Edit keinen Kameraton. | Kliplerinin hiçbirinde ses yok, o yüzden bu düzenleme kamera sesini kullanmıyor. |  |
| None of your clips has sound. | Keiner deiner Clips hat Ton. | Kliplerinin hiçbirinde ses yok. |  |
| None of your clips is too short to use. | Keiner deiner Clips ist zu kurz zum Verwenden. | Kliplerinden hiçbiri kullanılamayacak kadar kısa değil. |  |
| None of your clips show "{label}" | Keiner deiner Clips zeigt "{label}" | Kliplerinin hiçbirinde "{label}" yok |  |
| None of your clips show "{words}" | Keiner deiner Clips zeigt "{words}" | Kliplerinin hiçbirinde "{words}" yok |  |
| None of your lines made it in | Keine deiner Zeilen hat es ins Video geschafft | Cümlelerinden hiçbiri videoya girmedi |  |
| Not done: {text}. | Nicht erledigt: {text}. | Yapılmadı: {text}. |  |
| Not everything you asked for made it in:\n | Nicht alles, was du wolltest, hat es ins Video geschafft:\n | İstediklerinin hepsi videoya giremedi:\n |  |
| Not in your clips or your message. | Nicht in deinen Clips oder deiner Nachricht. | Kliplerinde ya da mesajında yok. |  |
| Not made yet: the draft is on hold. | Noch nicht erstellt: Der Entwurf ist angehalten. | Henüz yapılmadı: taslak beklemede. |  |
| Not used | Nicht verwendet | Kullanılmadı |  |
| Not used: you don't talk about {names_join_not_said}. | Nicht verwendet: Du sprichst nicht über {names_join_not_said}. | Kullanılmadı: {names_join_not_said} hakkında konuşmuyorsun. |  |
| Note:· | Hinweis:· | Not:· |  |
| Nothing changed for this | Dafür hat sich nichts geändert | Bunun için bir şey değişmedi |  |
| Nothing is made yet. Tell me what to do differently for: {listed}. | Es wurde noch nichts erstellt. Sag mir, was ich anders machen soll bei: {listed}. | Henüz bir şey yapılmadı. Şunlar için neyi farklı yapmamı istersin: {listed}. |  |
| Nothing is made yet. Tell me what to do differently. | Es wurde noch nichts erstellt. Sag mir, was ich anders machen soll. | Henüz bir şey yapılmadı. Neyi farklı yapmamı istediğini söyle. |  |
| Nothing is made yet; tell me what to do differently. | Es wurde noch nichts erstellt; sag mir, was ich anders machen soll. | Henüz bir şey yapılmadı; neyi farklı yapmamı istediğini söyle. |  |
| Nothing was changed. | Es wurde nichts geändert. | Hiçbir şey değişmedi. |  |
| Nothing was published. | Es wurde nichts veröffentlicht. | Yeni bir video oluşturulmadı. |  |
| On-device rendering isn't available for this account right now, so I left the video as it was. | Das Erstellen auf dem Gerät ist für dieses Konto gerade nicht verfügbar, deshalb habe ich das Video so gelassen, wie es war. | Cihaz üzerinde video oluşturma bu hesapta şu anda kullanılamıyor, o yüzden videoyu olduğu gibi bıraktım. |  |
| One caption line can't be sized or moved on its own from chat on this edit. | In diesem Edit kann ich eine einzelne Untertitelzeile nicht per Chat einzeln vergrößern oder verschieben. | Bu düzenlemede tek bir altyazı satırını sohbetten ayrı boyutlandıramıyorum ya da taşıyamıyorum. |  |
| One of those lines changed, so I left your video as it was. Refresh and try again. | Eine dieser Zeilen hat sich geändert, deshalb habe ich dein Video so gelassen, wie es war. Aktualisier und versuch es nochmal. | Satırlardan biri değişmiş, o yüzden videonu olduğu gibi bıraktım. Yenileyip tekrar dene. |  |
| One of your clips couldn't be downloaded. Reattach it and try again. | Einer deiner Clips konnte nicht heruntergeladen werden. Häng ihn nochmal an und versuch es erneut. | Kliplerinden biri indirilemedi. Yeniden ekle ve tekrar dene. |  |
| One of your clips couldn't be used (it may be corrupted or unsupported). Try a different clip. | Einer deiner Clips konnte nicht verwendet werden (er ist vielleicht beschädigt oder wird nicht unterstützt). Probier einen anderen Clip. | Kliplerinden biri kullanılamadı (bozuk ya da desteklenmiyor olabilir). Farklı bir klip dene. |  |
| One of your own clips can only end the video with its own sound, not silent | Einer deiner eigenen Clips kann das Video nur mit seinem eigenen Ton beenden, nicht stumm | Kendi kliplerinden biri videoyu ancak kendi sesiyle bitirebilir, sessiz değil |  |
| One photo or video stays behind you for the whole video; it doesn't change | Ein Foto oder Video bleibt das ganze Video über hinter dir; es wechselt nicht | Bütün video boyunca arkanda tek bir fotoğraf ya da video duruyor; değişmiyor |  |
| One photo or video stays behind you for the whole video; your iPhone can't switch backgrounds yet | Ein Foto oder Video bleibt das ganze Video über hinter dir; dein iPhone kann Hintergründe noch nicht wechseln | Bütün video boyunca arkanda tek bir fotoğraf ya da video duruyor; iPhone'un henüz arka planı değiştiremiyor |  |
| One render | Ein Video | Tek bir video |  |
| One stays on for {longest:.0f} s, but you asked for a change about every {cadence:g} s: there are only {shown} photos or videos for this talk and none repeats. Add more for faster changes. | Eins bleibt {longest:.0f} s im Bild, aber du wolltest etwa alle {cadence:g} s einen Wechsel: Für diese Rede gibt es nur {shown} Fotos oder Videos, und keins wiederholt sich. Füg mehr hinzu für schnellere Wechsel. | Biri {longest:.0f} sn ekranda kalıyor ama yaklaşık {cadence:g} saniyede bir değişmesini istedin: bu konuşma için yalnızca {shown} fotoğraf ya da video var ve hiçbiri tekrar etmiyor. Daha hızlı değişmesi için daha fazlasını ekle. |  |
| One thing didn't match your request: {issues_0}. I prepared a fix in your draft. Approve it to render, or undo it. | Eine Sache stimmte nicht mit deiner Anfrage überein: {issues_0}. Ich habe in deinem Entwurf eine Korrektur vorbereitet. Gib sie frei, um das Video zu erstellen, oder mach sie rückgängig. | Bir şey isteğinle uyuşmadı: {issues_0}. Taslağında bir düzeltme hazırladım. Oluşturmak için onayla ya da geri al. |  |
| Only a talking-to-camera edit made on your iPhone can end on a full-screen colour with your text | Nur ein Talking-Edit, das auf deinem iPhone erstellt wird, kann mit einem Vollbild in einer Farbe und deinem Text enden | Videoyu yazılı tam ekran bir renkle yalnızca iPhone'unda yapılan kameraya konuşma düzenlemesi bitirebilir |  |
| Only a talking-to-camera edit made on your iPhone can put a photo or video behind you | Nur ein Edit, in dem du in die Kamera sprichst und der auf deinem iPhone erstellt wird, kann ein Foto oder Video hinter dich legen | Arkana fotoğraf ya da video yalnızca iPhone'da hazırlanan konuşmalı düzenlemede konabilir |  |
| Only a talking-to-camera edit made on your iPhone can put a plain colour behind you | Nur ein Edit, in dem du in die Kamera sprichst und der auf deinem iPhone erstellt wird, kann eine einfarbige Fläche hinter dich legen | Arkana düz renk yalnızca iPhone'da hazırlanan konuşmalı düzenlemede konabilir |  |
| Only a talking-to-camera edit on your iPhone can end on another video | Nur ein Edit auf deinem iPhone, in dem du in die Kamera sprichst, kann mit einem anderen Video enden | Başka bir videoyla yalnızca iPhone'daki konuşmalı düzenleme bitebilir |  |
| Only a talking-to-camera edit on your iPhone can pause your talk for a clip | Nur ein Edit auf deinem iPhone, in dem du in die Kamera sprichst, kann deine Rede für einen Clip pausieren | Konuşmayı bir klip için yalnızca iPhone'daki konuşmalı düzenleme durdurabilir |  |
| Only clip {which} is in this edit and it runs about {possible:g} seconds, so the video can't be {wanted:g} seconds. | In diesem Edit ist nur Clip {which}, und er dauert etwa {possible:g} Sekunden, deshalb kann das Video nicht {wanted:g} Sekunden lang sein. | Bu düzenlemede yalnızca {which}. klip var ve yaklaşık {possible:g} saniye sürüyor, bu yüzden video {wanted:g} saniye olamıyor. |  |
| Only clips {which} are in this edit and together they run about {possible:g} seconds, so the video can't be {wanted:g} seconds. | In diesem Edit sind nur die Clips {which}, und zusammen dauern sie etwa {possible:g} Sekunden, deshalb kann das Video nicht {wanted:g} Sekunden lang sein. | Bu düzenlemede yalnızca {which}. klipler var ve birlikte yaklaşık {possible:g} saniye sürüyor, bu yüzden video {wanted:g} saniye olamıyor. |  |
| Only one clip can play on after your voice | Nur ein Clip kann nach deiner Stimme weiterlaufen | Seslendirmeden sonra yalnızca tek bir klip devam edebilir |  |
| Only one of your own clips can pause the talk, not a video from Visuals yet | Nur einer deiner eigenen Clips kann die Rede pausieren, noch kein Video aus Visuals | Konuşmayı yalnızca kendi kliplerinden biri durdurabilir, Visuals videosu değil |  |
| Only one photo or video is attached, so it stays behind you for the whole video | Es ist nur ein Foto oder Video angehängt, deshalb bleibt es das ganze Video über hinter dir | Yalnızca bir fotoğraf ya da video ekli, o yüzden bütün video boyunca arkanda duruyor |  |
| Only part of "{line}" is in the edit | Nur ein Teil von "{line}" ist im Edit | "{line}" sözünün sadece bir kısmı düzenlemede |  |
| Only preview copies of your clips are uploaded (the originals stay on your iPhone), so this edit has to be made on your iPhone. Your edit is saved. Open the project on the iPhone that has the clips and ask Kria to make it again. | Hochgeladen sind nur Vorschaukopien deiner Clips (die Originale bleiben auf deinem iPhone), deshalb muss dieser Edit auf deinem iPhone erstellt werden. Dein Edit ist gespeichert. Öffne das Projekt auf dem iPhone mit den Clips und bitte Kria, es nochmal zu erstellen. | Kliplerinin yalnızca önizleme kopyaları yüklü (orijinaller iPhone'unda duruyor), bu yüzden bu düzenleme iPhone'unda yapılmalı. Düzenlemen kayıtlı. Projeyi klipler olan iPhone'da aç ve Kria'dan videoyu yeniden yapmasını iste. |  |
| Only the last clip can play on after your voice, and that isn't the last clip in this edit | Nur der letzte Clip kann nach deiner Stimme weiterlaufen, und das ist in diesem Edit nicht der letzte Clip | Seslendirmeden sonra yalnızca son klip devam edebilir ve bu düzenlemede o klip son değil |  |
| Only {len_entry_targets} of {total} texts changed: {phrase} | Nur {len_entry_targets} von {total} Texten wurden geändert: {phrase} | {total} yazıdan yalnızca {len_entry_targets} tanesi değişti: {phrase} |  |
| Only {len_numbers} of your {own_count} clips are in this edit and together they run about {possible:g} seconds, so the video can't be {wanted:g} seconds. | Nur {len_numbers} deiner {own_count} Clips sind in diesem Edit und zusammen laufen sie etwa {possible:g} Sekunden, deshalb kann das Video nicht {wanted:g} Sekunden lang sein. | Bu düzenlemede {own_count} klipten yalnızca {len_numbers} tanesi var ve birlikte yaklaşık {possible:g} saniye sürüyor, bu yüzden video {wanted:g} saniye olamıyor. |  |
| Only {shown} of your {asked} photos or videos take turns behind you; I couldn't use the rest. | Nur {shown} deiner {asked} Fotos oder Videos wechseln sich hinter dir ab; den Rest konnte ich nicht verwenden. | {asked} fotoğraf ya da videodan yalnızca {shown} tanesi arkanda sırayla değişiyor; gerisini kullanamadım. |  |
| Only {shown} of your {asked} photos or videos take turns over the colour; I couldn't use the rest. | Nur {shown} deiner {asked} Fotos oder Videos wechseln sich über der Farbe ab; den Rest konnte ich nicht verwenden. | {asked} fotoğraf ya da videodan yalnızca {shown} tanesi rengin üstünde sırayla değişiyor; gerisini kullanamadım. |  |
| Open the specific video version you want to edit, then ask me again. | Öffne die Videoversion, die du bearbeiten möchtest, und frag mich dann nochmal. | Düzenlemek istediğin video sürümünü aç, sonra bana tekrar sor. |  |
| Open the video version you want to edit, then ask me to remove its uploaded Visuals. | Öffne die Videoversion, die du bearbeiten möchtest, und bitte mich dann, die hochgeladenen Visuals zu entfernen. | Düzenlemek istediğin video sürümünü aç, sonra yüklediğin görselleri kaldırmamı iste. |  |
| Open with {strongest}; it gives the story an immediate visual point of view. | Starte mit {strongest}; so bekommt die Story sofort eine klare visuelle Perspektive. | {strongest} ile aç; hikâye ilk andan net bir görsel bakış açısı kazanır. |  |
| Opens where “{part_unheard}” is said. | Beginnt dort, wo „{part_unheard}“ gesagt wird. | “{part_unheard}” denen yerden açılır. |  |
| Optional: move the {word} to the bottom so it's easier to read. | Optional: {word} nach unten verschieben, damit der Text besser lesbar ist. | İstersen {word} daha kolay okunsun diye alta taşınabilir. |  |
| Optional: move the {word} to the middle so it's easier to read. | Optional: {word} in die Mitte verschieben, damit der Text besser lesbar ist. | İstersen {word} daha kolay okunsun diye ortaya taşınabilir. |  |
| Optional: move the {word} to the top so it's easier to read. | Optional: {word} nach oben verschieben, damit der Text besser lesbar ist. | İstersen {word} daha kolay okunsun diye üste taşınabilir. |  |
| Optional: put your exact wording back on the {word}. | Optional: Setz deinen genauen Wortlaut wieder auf {word}. | İstersen {word} tam yazdığın hâline döndürülebilir. |  |
| Optional: shorten the {word} so it ends before the video does. | Optional: Kürz {word}, damit es endet, bevor das Video endet. | İstersen {word} video bitmeden bitecek şekilde kısaltılabilir. |  |
| Original camera audio is required by the render contract. | Der Originalton der Kamera ist für die Videoerstellung vorgeschrieben. | Oluşturma sözleşmesi kameranın özgün sesini zorunlu tutuyor. |  |
| Our AI took too long to plan this edit, so your video wasn't made. Your edit and footage are saved. Try again in a moment. | Unsere KI hat zu lange gebraucht, um diesen Edit zu planen, deshalb wurde dein Video nicht erstellt. Dein Edit und dein Material sind gespeichert. Versuch es gleich nochmal. | Yapay zekâ bu düzenlemeyi planlarken çok uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Birazdan tekrar dene. |  |
| Part of a split screen can't go full screen from chat on this edit. | In diesem Edit kann ich einen Teil des Split-Screens nicht per Chat auf Vollbild stellen. | Bu düzenlemede bölünmüş ekranın bir kısmını sohbetten tam ekran yapamıyorum. |  |
| Partly | Teilweise | Kısmen |  |
| Partly done. | Teilweise erledigt. | Kısmen yapıldı. |  |
| Photo and sound pop-ins timed to your words aren't available for this edit yet | Foto- und Ton-Einblendungen, die auf deine Worte abgestimmt sind, gibt es für diesen Edit noch nicht | Sözlerine göre çıkan fotoğraf ve ses efektleri bu düzenleme için henüz yok |  |
| Photo {n} | Foto {n} | Fotoğraf {n} |  |
| Photos and videos from Visuals can't be timed to a voiceover here yet, so this edit uses your project's own clips. | Fotos und Videos aus Visuals lassen sich hier noch nicht auf ein Voiceover abstimmen, deshalb nutzt dieser Edit die eigenen Clips deines Projekts. | Visuals'taki fotoğraf ve videolar henüz seslendirmeye göre zamanlanamıyor, o yüzden bu düzenleme projenin kendi kliplerini kullanıyor. |  |
| Photos timed to your words don't show yet when several of your clips talk, so this video won't have them | Auf deine Worte abgestimmte Fotos werden noch nicht gezeigt, wenn in mehreren deiner Clips gesprochen wird, deshalb hat dieses Video sie nicht | Birkaç klibinde konuştuğunda sözlerine göre çıkan fotoğraflar henüz gösterilemiyor, o yüzden bu videoda olmayacak |  |
| Pick a clip where you talk to use as the voice, or ask for a plain montage. | Wähl einen Clip, in dem du sprichst, als Stimme, oder wünsch dir eine einfache Montage. | Ses olarak kullanmak için konuştuğun bir klip seç ya da düz bir montaj iste. |  |
| Pick a clip where you talk, record a voiceover, or ask for a montage. | Wähl einen Clip, in dem du sprichst, nimm ein Voiceover auf oder wünsch dir eine Montage. | Konuştuğun bir klip seç, bir seslendirme kaydet ya da bir montaj iste. |  |
| Pick a different clip to carry the speech, or ask for a plain montage. | Wähl einen anderen Clip, der die Sprache trägt, oder wünsch dir eine einfache Montage. | Konuşmayı taşıyacak başka bir klip seç ya da düz bir montaj iste. |  |
| Place and time labels can't describe what each clip shows. Ask me to 'write a short caption for each clip' and I'll write them from what I saw in the footage. | Orts- und Zeit-Labels können nicht beschreiben, was jeder Clip zeigt. Bitte mich, 'für jeden Clip einen kurzen Untertitel zu schreiben', dann schreibe ich sie nach dem, was ich im Material gesehen habe. | Yer ve saat etiketleri her klibin ne gösterdiğini anlatamaz. Bana 'her klip için kısa bir yazı yaz' de, çekimlerde gördüklerime göre yazayım. |  |
| Place {role} text at {where} in future videos. | In künftigen Videos {role} an Position „{where}“ setzen. | Gelecek videolarda {role} {where} kısımda dursun. |  |
| Planning took too long | Die Planung hat zu lange gedauert | Planlama çok uzun sürdü |  |
| Playing a clip in the middle of your talk isn't available yet | Einen Clip mitten in deiner Rede abzuspielen gibt es noch nicht | Konuşmanın ortasında klip oynatmak henüz yok |  |
| Playing another clip in your talk needs a Talking edit with several clips | Um einen anderen Clip in deiner Rede abzuspielen, braucht es einen Talking-Edit mit mehreren Clips | Konuşmanda başka klip oynatmak için birden çok klipli düzenleme gerekiyor |  |
| Playing one clip's own sound under or after your voice only works in a voiceover edit made on your iPhone, not in this one | Den Originalton eines Clips unter oder nach deiner Stimme abzuspielen geht nur in einem Voiceover-Edit, der auf deinem iPhone erstellt wurde, nicht in diesem | Bir klibin kendi sesini seslendirmenin altında ya da sonrasında çalmak yalnızca iPhone'unda yapılan seslendirmeli düzenlemede mümkün, bu düzenlemede değil |  |
| Playing that clip on after your voice would take the video past the length you asked for | Wenn dieser Clip nach deiner Stimme weiterliefe, würde das Video länger als die gewünschte Länge | O klibi seslendirmeden sonra çalmak videoyu istediğin uzunluğun dışına çıkarırdı |  |
| Please clarify the requested changes so I can prepare the edit again. | Bitte erklär die gewünschten Änderungen genauer, damit ich den Edit nochmal vorbereiten kann. | Düzenlemeyi yeniden hazırlayabilmem için istediğin değişiklikleri netleştir. |  |
| Please restate the complete clip instructions in a shorter message so I can preserve all of them. | Schreib die kompletten Clip-Anweisungen bitte in einer kürzeren Nachricht nochmal, damit ich keine verliere. | Klip talimatlarının hepsini daha kısa bir mesajda yeniden yazar mısın? Hiçbirini kaçırmak istemiyorum. |  |
| Please shorten this Visuals request to 2,000 characters so I can apply it safely. | Kürz diese Visuals-Anfrage bitte auf 2.000 Zeichen, damit ich sie sicher anwenden kann. | Bu görsel isteğini güvenle uygulayabilmem için 2.000 karakterin altına kısaltır mısın? |  |
| Please shorten this request to 2,000 characters so I can apply it safely. | Kürz diese Anfrage bitte auf 2.000 Zeichen, damit ich sie sicher anwenden kann. | Bu isteği güvenle uygulayabilmem için 2.000 karakterin altına kısaltır mısın? |  |
| Pop-in on {joined} | Pop-in: {joined} | {join_parts_tr}{tail_tr} çıkıyor |  |
| Pop-ins on {joined} | Pop-ins: {joined} | {join_parts_tr}{tail_tr} çıkıyor |  |
| Portuguese | Portugiesisch |  |  |
| Prepared adjusted text timing while preserving the original wording and appearance. | Angepasstes Text-Timing vorbereitet; Wortlaut und Aussehen sind unverändert geblieben. | Özgün kelimeleri ve görünümü koruyarak yazı zamanlaması taslağını hazırladım. |  |
| Prepared the timing-only draft with the original wording unchanged. I removed unrequested copy changes from the proposed edit. | Entwurf nur mit geändertem Timing vorbereitet, der ursprüngliche Wortlaut ist unverändert. Ich habe ungewollte Textänderungen aus dem vorgeschlagenen Edit entfernt. | Özgün kelimeleri değiştirmeden yalnızca zamanlama taslağını hazırladım. İstenmeyen metin değişikliklerini önerilen düzenlemeden çıkardım. |  |
| Preserve the current word timings | Aktuelle Wort-Timings beibehalten | Mevcut kelime zamanlamalarını koru |  |
| Processing timed out — your clips are heavy (likely 4K/HDR). Try fewer or shorter clips. | Die Verarbeitung hat zu lange gedauert – deine Clips sind sehr groß (wahrscheinlich 4K/HDR). Probier weniger oder kürzere Clips. | İşlem zaman aşımına uğradı; kliplerin çok ağır (muhtemelen 4K/HDR). Daha az ya da daha kısa klip dene. |  |
| Put {ref} after "{row_after_text}" anyway? | {ref} trotzdem nach "{row_after_text}" setzen? | {ref} yine de "{row_after_text}" cümlesinden sonra koyayım mı? |  |
| Putting a photo or video behind you isn't available on your iPhone yet | Ein Foto oder Video hinter dich zu legen ist auf deinem iPhone noch nicht verfügbar | Arkana fotoğraf ya da video koymak iPhone'unda henüz kullanılamıyor |  |
| Putting a plain colour behind you isn't available on your iPhone yet | Eine einfarbige Fläche hinter dich zu legen ist auf deinem iPhone noch nicht verfügbar | Arkana düz renk koymak iPhone'unda henüz kullanılamıyor |  |
| Re-cropped {who} | {who} neu zugeschnitten | {who} yeniden kırpıldı |  |
| Reading this as a montage in the day-vlog style. | Ich verstehe das als Montage im Tagesvlog-Stil. | Bunu günlük vlog tarzında bir montaj olarak okudum. |  |
| Reading this as a montage in the single-hero style. | Ich verstehe das als Montage im Hauptclip-Stil. | Bunu tek kahraman tarzında bir montaj olarak okudum. |  |
| Record a voiceover first, then I can render this direction. | Nimm zuerst ein Voiceover auf, dann kann ich diese Richtung als Video erstellen. | Önce bir seslendirme kaydet, sonra bu yönde videoyu oluşturabilirim. |  |
| Record it again and check that nothing is covering the microphone. | Nimm es nochmal auf und achte darauf, dass nichts das Mikrofon verdeckt. | Yeniden kaydet ve mikrofonun üstünü hiçbir şeyin kapatmadığından emin ol. |  |
| Record it with “Record with this script” or import your own voiceover, then I'll plan the narrated edit around it. | Nimm es mit „Record with this script“ auf oder importier dein eigenes Voiceover, dann plane ich den Narrated-Edit darum herum. | “Bu metinle kaydet” ile kaydet ya da kendi seslendirmeni içe aktar, sonra anlatımlı düzenlemeyi ona göre planlayayım. |  |
| Record or upload your voice, or tell me to use music instead. | Nimm deine Stimme auf oder lade sie hoch, oder sag mir, dass ich stattdessen Musik nehmen soll. | Sesini kaydet ya da yükle; istersen bunun yerine müzik kullanmamı söyle. |  |
| Record your voice for this edit, or ask for a different format. | Nimm für diesen Edit deine Stimme auf oder wünsch dir ein anderes Format. | Bu düzenleme için sesini kaydet ya da farklı bir format iste. |  |
| Recording is empty | Die Aufnahme ist leer | Kayıt boş |  |
| Recording is too long — keep it under two minutes | Die Aufnahme ist zu lang – halt sie unter zwei Minuten | Kayıt çok uzun, iki dakikanın altında tut |  |
| Relaxed pacing | Tempo: Entspannt | Tempo: Sakin |  |
| Remembered for future videos. | Für künftige Videos gemerkt. | Bunu sonraki videoların için aklımda tutacağım. |  |
| Removed the caption animation | Untertitel-Animation entfernt | Altyazı animasyonu kaldırıldı |  |
| Removed the caption outline | Untertitel-Kontur entfernt | Altyazı konturu kaldırıldı |  |
| Removed the caption shadow | Untertitel-Schatten entfernt | Altyazı gölgesi kaldırıldı |  |
| Removed the entrance animation from {who} | {who}: Eingangsanimation entfernt | {who} için giriş animasyonu kaldırıldı |  |
| Removed the music | Musik entfernt | Müzik kaldırıldı |  |
| Removed the requested uploaded Visuals from the saved edit. Text, captions, footage and audio are unchanged. | Die gewünschten hochgeladenen Visuals wurden aus dem gespeicherten Edit entfernt. Text, Untertitel, Material und Ton sind unverändert. | İstediğin yüklenmiş görselleri kayıtlı kurgudan kaldırdım. Yazılar, altyazılar, görüntüler ve ses aynı kaldı. |  |
| Removed visual items | Visuelle Elemente entfernt | Görsel öğeler kaldırıldı |  |
| Removed your song | Deinen Song entfernt | Şarkın kaldırıldı |  |
| Removed {what} | {what} entfernt | {what} kaldırıldı |  |
| Removed {who} | {who} entfernt | {who} kaldırıldı |  |
| Rendering is unavailable for this edit right now. | Das Erstellen des Videos ist für diesen Edit gerade nicht verfügbar. | Bu düzenleme şu anda oluşturulamıyor. |  |
| Rendering ran out of memory — your clips are heavy. Try fewer or shorter clips. | Beim Erstellen des Videos ging der Speicher aus – deine Clips sind sehr groß. Probier weniger oder kürzere Clips. | Video hazırlanırken bellek yetmedi; kliplerin çok ağır. Daha az ya da daha kısa klip dene. |  |
| Reordered the clips | Clips neu angeordnet | Klipler yeniden sıralandı |  |
| Reset the zoom of {who} | {who}: Zoom zurückgesetzt | {who} yakınlaştırması sıfırlandı |  |
| Resized {who} | {who}: Größe geändert | {who} yeniden boyutlandı |  |
| Restored your exact wording on the {word}. | Dein genauer Wortlaut bei {word} ist wiederhergestellt. | {role_at_start_role} tam yazdığın hâline döndü. |  |
| Retime text to {closest:g}s | Text auf {closest:g} s verteilen | Yazıları {closest:g} saniyeye yay |  |
| Retime text to {exc_requested_duration_s:g}s | Text auf {exc_requested_duration_s:g} s verteilen | Yazıları {exc_requested_duration_s:g} saniyeye yay |  |
| Retimed {len_ids} words in sequence across {total:g}s, keeping their wording and appearance. | {len_ids} Wörter der Reihe nach auf {total:g} s verteilt, Wortlaut und Aussehen bleiben gleich. | {len_ids} kelimeyi metin ve görünümünü koruyarak {total:g} saniyeye sırayla yaydım. |  |
| Retimed {len_ids} words in sequence from {start:g}s to {end:g}s. | {len_ids} Wörter der Reihe nach von {start:g} s bis {end:g} s verteilt. | {len_ids} kelimeyi {start:g} ile {end:g} saniye arasında sırayla yaydım. |  |
| Retimed {who} | Timing geändert: {who} | {who} zamanlaması değişti |  |
| Retimed {who} ({span_first_before} → {span_first_after}) | Timing geändert: {who} ({span_first_before} → {span_first_after}) | {who} zamanlaması değişti ({span_first_before} → {span_first_after}) |  |
| Review the restored text for {update_description}: "{update_literal}" | Prüf den wiederhergestellten Text für {update_description}: "{update_literal}" | Geri yüklenen metni gözden geçir ({update_description}): "{update_literal}" |  |
| Rewrote {who} | Neu geschrieben: {who} | {who} yeniden yazıldı |  |
| Save your edits first, then I can cut the silences. | Speichere erst deine Änderungen, dann kann ich die Pausen schneiden. | Önce düzenlemelerini kaydet, sonra sessiz kısımları kesebilirim. |  |
| Save your edits first, then tap this suggestion again. Nothing was changed. | Speichere erst deine Änderungen und tipp dann nochmal auf diesen Vorschlag. Es wurde nichts geändert. | Önce düzenlemelerini kaydet, sonra bu öneriye tekrar dokun. Hiçbir şey değişmedi. |  |
| Saving your upload didn't finish. Try again. | Das Speichern deines Uploads wurde nicht fertig. Versuch es nochmal. | Yüklemen kaydedilemedi. Tekrar dene. |  |
| Serbian | Serbisch |  |  |
| Set the video to {end:g}s while preserving the text timing. | Das Video auf {end:g} s setzen und das Timing der Texte beibehalten. | Yazıların zamanlamasını koruyarak videoyu {end:g} saniye yap. |  |
| Set {who} to Title Case | {who}: Jedes Wort beginnt jetzt groß | {who} → Baş Harfler Büyük |  |
| Set {who} to UPPERCASE | {who} in GROSSBUCHSTABEN gesetzt | {who} → BÜYÜK HARF |  |
| Set {who} to a new case | {who}: Schreibweise geändert | {who} → yeni harf biçimi |  |
| Set {who} to its original case | {who} wieder in der ursprünglichen Schreibweise | {who} → özgün harf |  |
| Set {who} to lowercase | {who} in kleinbuchstaben gesetzt | {who} → küçük harf |  |
| Set {who} to {common:g}s | {who} auf {common:g} s gesetzt | {who} {common:g} sn yapıldı |  |
| Shortened the {word} so it ends before the video does. | {word} gekürzt, damit es vor dem Ende des Videos aufhört. | {role_at_start_role} video bitmeden bitecek şekilde kısaldı. |  |
| Should I make a simpler version? | Soll ich eine einfachere Version machen? | Daha sade bir sürüm yapayım mı? |  |
| Should I make it without these, or do you want to change your request? | Soll ich es ohne diese erstellen, oder möchtest du deine Anfrage ändern? | Bunlar olmadan mı hazırlayayım, yoksa isteğini mi değiştirmek istersin? |  |
| Shown:· | Gezeigt:· | Gösterildi:· |  |
| Simpler version with the agreed omissions. | Einfachere Version mit den besprochenen Auslassungen. | Kararlaştırılan eksiklerle daha basit sürüm. |  |
| Simpler video with the supported edits. | Einfacheres Video mit den unterstützten Änderungen. | Desteklenen düzenlemelerle daha basit video. |  |
| Single Hero format | Format: Hauptclip | Format: Tek kahraman klip |  |
| Single-hero | Hauptclip | Tek kahraman |  |
| Slides format | Format: Folien | Format: Slayt |  |
| Small card of {name} at {card_at_s:.1f} s for {hold:.1f} s | Kleine Karte von {name} bei {card_at_s:.1f} s für {hold:.1f} s | {name} {card_at_s:.1f} sn'de {hold:.1f} sn küçük kart olarak |  |
| Small card of {name} during the clip you named for {hold:.1f} s | Kleine Karte von {name} während des genannten Clips für {hold:.1f} s | {name} söylediğin klipte {hold:.1f} sn küçük kart olarak |  |
| Small cards at an exact time aren't available for this edit yet | Kleine Karten zu einer genauen Zeit gibt es für diesen Edit noch nicht | Tam zamanlı küçük kartlar bu düzenleme için henüz yok |  |
| Small cards at an exact time aren't available for this edit yet; left them out. | Kleine Karten zu einer genauen Zeit gibt es für diesen Edit noch nicht; habe sie weggelassen. | Tam zamanlı küçük kartlar bu düzenleme için henüz yok; koymadım. |  |
| Some moments are your call: ones you said aren't in your clips, or let me pick | Manche Momente entscheidest du: die, die laut dir nicht in deinen Clips sind, oder die, die du mir überlassen hast | Bazı anlar senin kararın: kliplerinde olmadığını söylediklerin ya da bana bıraktıkların |  |
| Some of the selected clips are no longer attached to this edit, so I can't read their footage. Select your clips again, or add them back. | Einige der ausgewählten Clips gehören nicht mehr zu diesem Edit, deshalb kann ich ihr Material nicht lesen. Wähl deine Clips nochmal aus oder füg sie wieder hinzu. | Seçili kliplerden bazıları artık bu düzenlemeye bağlı değil, bu yüzden çekimlerini okuyamıyorum. Klipleri yeniden seç ya da tekrar ekle. |  |
| Some of the selected clips are shorter than 0.1 seconds. Replace them, or leave them out of this edit. | Einige der ausgewählten Clips sind kürzer als 0,1 Sekunden. Ersetze sie oder lass sie in diesem Edit weg. | Seçili kliplerden bazıları 0,1 saniyeden kısa. Onları değiştir ya da bu videoya alma. |  |
| Some of your clips have no filming time, so I couldn't put them in the order you filmed them. | Bei einigen deiner Clips fehlt die Aufnahmezeit, deshalb konnte ich sie nicht in der Reihenfolge deiner Aufnahmen anordnen. | Bazı kliplerinde çekim saati yok, o yüzden onları çektiğin sıraya koyamadım. |  |
| Some requested text ends before the video does. | Manche Texte enden vor dem Ende des Videos. | Bazı yazılar video bitmeden kayboluyor. |  |
| Some text still covers {named}; there was no spot clear of it | Manche Texte liegen noch über {named}; es gab keine freie Stelle | Bazı yazılar hâlâ {named_tr} üstünde; açıkta bir yer kalmadı |  |
| Something broke while planning this edit, so your video wasn't made. Your edit and footage are saved. Try again, or ask for a change. | Beim Planen dieses Edits ist etwas schiefgegangen, deshalb wurde dein Video nicht erstellt. Dein Edit und dein Material sind gespeichert. Versuch es nochmal oder wünsch dir eine Änderung. | Bu düzenleme planlanırken bir şey bozuldu, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Tekrar dene ya da bir değişiklik iste. |  |
| Something changed while exporting. Try again. | Beim Exportieren hat sich etwas geändert. Versuch es nochmal. | Dışa aktarma sırasında bir şey değişti. Tekrar dene. |  |
| Something went wrong and this render didn't finish. Your direction and footage are still saved — try again. | Etwas ist schiefgegangen und dieses Video wurde nicht fertig. Deine Vorgaben und dein Material sind noch gespeichert – versuch es nochmal. | Bir şeyler ters gitti ve bu video tamamlanmadı. Yönün ve çekimlerin hâlâ kayıtlı; tekrar dene. |  |
| Something went wrong while assembling your video. Try again. | Beim Zusammensetzen deines Videos ist etwas schiefgegangen. Versuch es nochmal. | Videon birleştirilirken bir şeyler ters gitti. Tekrar dene. |  |
| Something you approved changed before this edit could render, so I didn't make it. Ask Kria to prepare the edit again. | Etwas, das du freigegeben hast, hat sich geändert, bevor dieser Edit erstellt werden konnte, deshalb habe ich ihn nicht gemacht. Bitte Kria, den Edit nochmal vorzubereiten. | Onayladığın bir şey bu düzenleme oluşturulmadan önce değişti, bu yüzden videoyu yapmadım. Kria'dan düzenlemeyi yeniden hazırlamasını iste. |  |
| Sound and photo pop-ins timed to your words aren't available for this edit yet; left them out. | Ton- und Foto-Einblendungen, die auf deine Worte abgestimmt sind, gibt es für diesen Edit noch nicht; habe sie weggelassen. | Sözlerine göre çıkan ses ve fotoğraflar bu düzenleme için henüz yok; koymadım. |  |
| Sound effects can't render on your iPhone yet. Ask for this edit without the sound effect. | Soundeffekte lassen sich auf deinem iPhone noch nicht erstellen. Wünsch dir diesen Edit ohne den Soundeffekt. | Ses efektleri iPhone'unda henüz hazırlanamıyor. Bu düzenlemeyi ses efektsiz iste. |  |
| Sound effects on iPhone are placed at the moments you named; the general sound-effect treatment was left out. | Soundeffekte auf dem iPhone werden an die Momente gesetzt, die du genannt hast; die allgemeine Soundeffekt-Gestaltung habe ich weggelassen. | iPhone'da ses efektleri senin söylediğin anlara konuyor; genel ses efekti uygulamasını koymadım. |  |
| Source | Quelle | Kaynak |  |
| Sourced on-screen explanations (shown only if you approve this draft): | Erklärungen mit Quellen auf dem Bildschirm (nur wenn du diesen Entwurf freigibst): | Kaynaklı ekran açıklamaları (yalnızca bu taslağı onaylarsan eklenir): |  |
| Sources could not be confirmed before approval: {prepared_reason} | Die Quellen konnten vor der Freigabe nicht bestätigt werden: {prepared_reason} | Kaynaklar onaydan önce doğrulanamadı: {prepared_reason} |  |
| Spanish | Spanisch |  |  |
| Speech cleanup cut only the long pauses, so shorter pauses stay | Die Sprachbereinigung hat nur die langen Pausen geschnitten, kürzere Pausen bleiben | Konuşma temizliği yalnızca uzun duraklamaları kesti, daha kısa duraklamalar kalıyor |  |
| Speech cleanup cut the long pauses | Die Sprachbereinigung hat die langen Pausen geschnitten | Konuşma temizliği uzun duraklamaları kesti |  |
| Speech cleanup cut what it heard as {joined} ({seconds:.1f} s). | Die Sprachbereinigung hat geschnitten, was sie dafür hielt: {joined} ({seconds:.1f} s). | Konuşma temizliği duyduğu şu sesleri kesti: {joined_tr} ({seconds_tr} sn). |  |
| Speech cleanup cut what it heard as {joined}. | Die Sprachbereinigung hat geschnitten, was sie dafür hielt: {joined}. | Konuşma temizliği duyduğu şu sesleri kesti: {joined_tr}. |  |
| Speech cleanup cut your pauses tight, but | Die Sprachbereinigung hat deine Pausen eng geschnitten, aber | Konuşma temizliği duraklamaları sıkılaştırdı ama |  |
| Speech cleanup cuts every pause down to about 0.3 s, and I'll tell you after if any stay longer | Die Sprachbereinigung kürzt jede Pause auf etwa 0,3 s, und ich sage dir danach, ob welche länger bleiben | Konuşma temizliği her duraklamayı yaklaşık 0,3 sn'ye kadar kısaltır, daha uzun kalan olursa sonra sana söylerim |  |
| Speech cleanup cuts only the long pauses in this kind of video, so shorter pauses stay | Die Sprachbereinigung schneidet bei dieser Art Video nur die langen Pausen, kürzere Pausen bleiben | Konuşma temizliği bu tür videoda yalnızca uzun duraklamaları keser, daha kısa duraklamalar kalır |  |
| Speech cleanup cuts the long pauses | Die Sprachbereinigung schneidet die langen Pausen | Konuşma temizliği uzun duraklamaları keser |  |
| Speech cleanup didn't find a sentence you restarted, so it may still be in your video | Die Sprachbereinigung hat keinen neu begonnenen Satz gefunden, deshalb kann er noch in deinem Video sein | Konuşma temizliği baştan başladığın bir cümle bulmadı, o yüzden videoda kalmış olabilir |  |
| Speech cleanup didn't run on this video, so the long pauses stay | Die Sprachbereinigung lief bei diesem Video nicht, deshalb bleiben die langen Pausen | Konuşma temizliği bu videoda çalışmadı, o yüzden uzun duraklamalar kaldı |  |
| Speech cleanup found no long pauses to cut | Die Sprachbereinigung hat keine langen Pausen zum Schneiden gefunden | Konuşma temizliği kesilecek uzun bir duraklama bulmadı |  |
| Speech cleanup is on, so some long pauses or filler sounds may be cut. | Die Sprachbereinigung ist an, deshalb werden eventuell einige lange Pausen oder Fülllaute geschnitten. | Konuşma temizliği açık, o yüzden bazı uzun duraklamalar ya da dolgu sesleri kesilebilir. |  |
| Speech cleanup isn't available for this project yet, so the pauses stay | Die Sprachbereinigung gibt es für dieses Projekt noch nicht, deshalb bleiben die Pausen | Konuşma temizliği bu proje için henüz yok, o yüzden duraklamalar kalıyor |  |
| Speech cleanup was off, so I couldn't cut the end and the video runs to the end of your take | Die Sprachbereinigung war aus, deshalb konnte ich das Ende nicht schneiden und das Video läuft bis zum Ende deines Takes | Konuşma temizliği kapalıydı, o yüzden sonu kesemedim ve video çekimin sonuna kadar sürüyor |  |
| Speech cleanup was off, so I couldn't cut the start and the video opens at the beginning of your take | Die Sprachbereinigung war aus, deshalb konnte ich den Anfang nicht schneiden und das Video beginnt am Anfang deines Takes | Konuşma temizliği kapalıydı, o yüzden başı kesemedim ve video çekimin başından açılıyor |  |
| Started {who} earlier in its footage | {who} beginnt jetzt früher im Material | {who} görüntüde daha erken başlıyor |  |
| Still in your video | Noch in deinem Video | Videonda hâlâ var |  |
| Still in your video: "{quote}" ({v1}) | Noch in deinem Video: „{quote}“ ({v1}) | {v2}: "{quote}" ({v1}) |  |
| Stopped using that preference for future videos. | Diese Vorliebe nutze ich für künftige Videos nicht mehr. | Bu tercihi sonraki videolarında artık kullanmayacağım. |  |
| Stopped. Working on your newer message instead. | Gestoppt. Ich arbeite stattdessen an deiner neueren Nachricht. | Durdurdum. Bunun yerine yeni mesajın üzerinde çalışıyorum. |  |
| Straightened {who} | Gerade gerichtet: {who} | {who} düzeltildi |  |
| Subtitled format | Format: Mit Untertiteln | Format: Altyazılı |  |
| Taking away the full-screen opening isn't available on this edit yet. | Den Vollbild-Anfang zu entfernen ist in diesem Edit noch nicht möglich. | Tam ekran açılışı kaldırmak bu düzenlemede henüz yok. |  |
| Talking Head format | Format: Talking Head | Format: Kameraya konuşma |  |
| Talking edits caption what you say, so I didn't add separate labels or captions to individual clips. | Talking-Edits zeigen das, was du sagst, als Untertitel, deshalb habe ich einzelnen Clips keine eigenen Labels oder Untertitel hinzugefügt. | Konuşmalı düzenlemeler söylediklerini altyazıya çevirir, o yüzden tek tek kliplere ayrı etiket ya da altyazı eklemedim. |  |
| Talking edits show your words as captions, so I can't add a title on top yet. Should I make it without the title? | Talking-Edits zeigen deine Worte als Untertitel, deshalb kann ich oben noch keinen Titel einfügen. Soll ich es ohne Titel machen? | Konuşmalı düzenlemeler sözlerini altyazı olarak gösterir, o yüzden üstüne henüz başlık ekleyemiyorum. Başlıksız yapayım mı? |  |
| Talking-to-camera videos render on your iPhone with exactly one clip. Remove the extra clips (or add one) and try again. No fallback edit was rendered. | Videos, in denen du in die Kamera sprichst, werden auf deinem iPhone mit genau einem Clip erstellt. Entferne die zusätzlichen Clips (oder füg einen hinzu) und versuch es nochmal. Es wurde kein Ersatz-Edit erstellt. | Kameraya konuşma videoları iPhone'unda tam olarak tek klipten oluşturulur. Fazla klipleri kaldır (ya da bir klip ekle) ve tekrar dene. Yedek bir düzenleme oluşturulmadı. |  |
| Talking-to-camera videos render on your iPhone with up to {max_clips} clips. Remove the extra clips and try again. No fallback edit was rendered. | Videos, in denen du in die Kamera sprichst, werden auf deinem iPhone mit bis zu {max_clips} Clips erstellt. Entferne die zusätzlichen Clips und versuch es nochmal. Es wurde kein Ersatz-Edit erstellt. | Kameraya konuşma videoları iPhone'unda en fazla {max_clips} klipten oluşturulur. Fazla klipleri kaldır ve tekrar dene. Yedek bir düzenleme oluşturulmadı. |  |
| Tap an option, or tell me in your own words. | Tipp auf eine Option oder sag es mir in deinen eigenen Worten. | Bir seçeneğe dokun ya da kendi sözlerinle anlat. |  |
| Tell me the colour as a name like red or a code like #FF0000 | Nenn mir die Farbe als Namen wie Rot oder als Code wie #FF0000 | Rengi kırmızı gibi bir adla ya da #FF0000 gibi bir kodla söyle |  |
| Tell me the words for the title, or say "continue without a title". | Sag mir den Text für den Titel oder schreib "Ohne Titel weiter". | Başlığın kelimelerini söyle ya da "başlık olmasın" yaz. |  |
| Tell me to redo it and I'll make a new version from what you already approved. | Sag mir, dass ich es nochmal machen soll, dann erstelle ich aus deiner Freigabe eine neue Version. | Baştan yapmamı söylersen onayladığın isteğe göre yeni bir sürüm hazırlarım. |  |
| Tell me what to change and I'll prepare it again. | Sag mir, was ich ändern soll, dann bereite ich es nochmal vor. | Neyi değiştirmemi istediğini söyle, yeniden hazırlayayım. |  |
| Tell me what you'd like to change and I'll try a different approach. | Sag mir, was du ändern möchtest, dann probiere ich es anders. | Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim. |  |
| Tell me where it should go | Sag mir, wohin es soll | Nereye koyayım, söyle |  |
| Tell me which clip you mean, and I'll make a new draft. | Sag mir, welchen Clip du meinst, dann mache ich einen neuen Entwurf. | Hangi klibi kastettiğini söyle, yeni bir taslak hazırlayayım. |  |
| Tell me which length you want. | Sag mir, welche Länge du möchtest. | Hangi uzunluğu istediğini söyle. |  |
| Tell me which option you want and I'll continue. | Sag mir, welche Option du möchtest, dann mache ich weiter. | Hangi seçeneği istediğini söyle, devam edeyim. |  |
| Text landed on {count} of {total} clips. | Text ist auf {count} von {total} Clips gelandet. | Yazı {total} klibin {count} tanesine geldi. |  |
| Text on {count} clips. | Text auf {count} Clips. | {count} klipte yazı var. |  |
| That approval expired before it was decided, so nothing was rendered. Tell me what you want and I'll prepare it again. | Diese Freigabe ist abgelaufen, bevor entschieden wurde, deshalb wurde kein Video erstellt. Sag mir, was du willst, dann bereite ich es nochmal vor. | Bu onay karara bağlanmadan süresi doldu, o yüzden hiçbir video oluşturulmadı. Ne istediğini söyle, yeniden hazırlayayım. |  |
| That approval was prepared for an earlier version of the video, so I cancelled it. Nothing was rendered; tell me what you want and I'll prepare it again. | Diese Freigabe war für eine frühere Version des Videos vorbereitet, deshalb habe ich sie abgebrochen. Es wurde kein Video erstellt; sag mir, was du willst, dann bereite ich es nochmal vor. | Bu onay videonun eski bir sürümü için hazırlanmıştı, o yüzden iptal ettim. Hiçbir video oluşturulmadı; ne istediğini söyle, yeniden hazırlayayım. |  |
| That change can't be rendered on your iPhone yet, so I left the video as it was. | Diese Änderung kann auf deinem iPhone noch nicht umgesetzt werden, deshalb habe ich das Video so gelassen, wie es war. | Bu değişiklik iPhone'unda henüz yapılamıyor, o yüzden videoyu olduğu gibi bıraktım. |  |
| That change is already reflected in the draft. | Diese Änderung ist im Entwurf schon drin. | Bu değişiklik taslakta zaten var. |  |
| That change isn't available for these sections yet. | Diese Änderung gibt es für diese Abschnitte noch nicht. | Bu değişiklik bu bölümler için henüz yapılamıyor. |  |
| That clip already plays somewhere else in the video | Dieser Clip läuft im Video schon an anderer Stelle | O klip videoda zaten başka bir yerde oynuyor |  |
| That clip didn't get its own text in this draft. | Dieser Clip hat in diesem Entwurf keinen eigenen Text bekommen. | O klibe bu taslakta kendi yazısı gelmedi. |  |
| That clip has no footage left after the part under your voice, so nothing plays on after it | Von diesem Clip ist nach dem Teil unter deiner Stimme nichts mehr übrig, deshalb läuft danach nichts weiter | O klipte seslendirmenin altında kalan kısımdan sonra görüntü kalmadı, o yüzden sonrasında bir şey çalmıyor |  |
| That clip has no sound of its own | Dieser Clip hat keinen eigenen Ton | O klibin kendi sesi yok |  |
| That clip has no sound of its own to play | Dieser Clip hat keinen eigenen Ton zum Abspielen | O klibin çalacak kendi sesi yok |  |
| That clip is already the one the video ends on | Mit diesem Clip endet das Video schon | O klip zaten videonun bittiği klip |  |
| That clip is in the edit, but not at the end | Dieser Clip ist im Edit, aber nicht am Ende | O klip düzenlemede var ama sonda değil |  |
| That clip is over 5 minutes, the longest a clip in your video can be | Dieser Clip ist länger als 5 Minuten, das ist die Höchstlänge für einen Clip in deinem Video | Bu klip 5 dakikadan uzun, videodaki bir klip en fazla bu kadar olabilir |  |
| That clip is too long to add at the end | Dieser Clip ist zu lang, um ihn am Ende einzufügen | Bu klip sona eklemek için çok uzun |  |
| That clip isn't in that spot in this edit | Dieser Clip ist in diesem Edit nicht an dieser Stelle | O klip bu düzenlemede o sırada değil |  |
| That clip isn't in the edit | Dieser Clip ist nicht im Edit | O klip düzenlemede yok |  |
| That clip isn't in this edit | Dieser Clip ist nicht in diesem Edit | O klip bu düzenlemede yok |  |
| That clip isn't one of the video clips in this edit, so I couldn't play its own sound | Dieser Clip gehört nicht zu den Videoclips in diesem Edit, deshalb konnte ich seinen eigenen Ton nicht abspielen | O klip bu düzenlemedeki video kliplerinden biri değil, o yüzden kendi sesini çalamadım |  |
| That clip plays on with its own sound after your voice ends | Dieser Clip läuft nach dem Ende deiner Stimme mit seinem eigenen Ton weiter | O klip, seslendirme bitince kendi sesiyle devam ediyor |  |
| That clip plays without its own sound, so your voice keeps playing over it | Dieser Clip läuft ohne eigenen Ton, deine Stimme läuft also weiter darüber | O klip kendi sesi olmadan oynuyor, sesin üzerinde çalmaya devam ediyor |  |
| That clip was asked for twice, so it plays once | Dieser Clip wurde zweimal gewünscht, deshalb läuft er nur einmal | O klip iki kez istendi, bir kez oynar |  |
| That clip's label repeated the clip before it, so I left it off. | Das Label dieses Clips war dasselbe wie beim Clip davor, deshalb habe ich es weggelassen. | O klibin etiketi önceki klibin etiketiyle aynıydı, o yüzden koymadım. |  |
| That clip's own sound plays softly under your voice | Der eigene Ton dieses Clips läuft leise unter deiner Stimme | O klibin kendi sesi seslendirmenin altında kısık çalıyor |  |
| That clip's text isn't the exact text you gave. | Der Text dieses Clips ist nicht der Text, den du angegeben hast. | O klibin yazısı verdiğin yazıyla aynı değil. |  |
| That edit didn't keep something you asked for. | Dieser Edit hat etwas nicht beibehalten, worum du gebeten hast. | Bu düzenleme istediğin bir şeyi korumadı. |  |
| That edit is based on an older draft. Refresh the editor and try again. | Diese Änderung basiert auf einem älteren Entwurf. Lade den Editor neu und versuch es nochmal. | Bu düzenleme taslağın eski bir halini temel alıyor. Editörü yenile ve tekrar dene. |  |
| That edit was not applied. | Diese Änderung wurde nicht übernommen. | Bu düzenleme uygulanmadı. |  |
| That exact text isn't in this draft. | Genau dieser Text ist in diesem Entwurf nicht drin. | Bu tam yazı bu taslakta yok. |  |
| That exact text isn't in this edit. | Genau dieser Text ist in diesem Edit nicht drin. | Bu tam yazı bu düzenlemede yok. |  |
| That footage cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem Material kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That footage cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem Material kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That is a photo, not a clip with sound | Das ist ein Foto, kein Clip mit Ton | Bu bir fotoğraf, sesli bir klip değil |  |
| That is a photo, not a video, so it has no sound of its own | Das ist ein Foto, kein Video, deshalb hat es keinen eigenen Ton | Bu bir fotoğraf, video değil; kendi sesi yok |  |
| That is more changes than I can apply in one go, so I left the video as it was. Ask for it in smaller steps, or for all of one kind of change at once (for example every font). | Das sind mehr Änderungen, als ich auf einmal umsetzen kann, deshalb habe ich das Video so gelassen, wie es war. Wünsch es dir in kleineren Schritten oder alle Änderungen einer Art auf einmal (zum Beispiel alle Schriften). | Bu, tek seferde uygulayabileceğimden fazla değişiklik; o yüzden videoyu olduğu gibi bıraktım. Daha küçük adımlarla iste ya da aynı türden tüm değişiklikleri tek seferde iste (örneğin tüm yazı tipleri). |  |
| That is the clip you speak in, so it can't also be added at the end | In diesem Clip sprichst du, deshalb kann er nicht zusätzlich am Ende eingefügt werden | Konuştuğun klip bu, o yüzden sona ayrıca eklenemez |  |
| That isn't a colour I can use. Say white, black, green, blue, red, yellow or grey, or give a hex like #FFFFFF. | Diese Farbe kann ich nicht verwenden. Sag Weiß, Schwarz, Grün, Blau, Rot, Gelb oder Grau, oder gib einen Hex-Code wie #FFFFFF an. | Bu, kullanabileceğim bir renk değil. Beyaz, siyah, yeşil, mavi, kırmızı, sarı ya da gri de, ya da #FFFFFF gibi bir kod ver. |  |
| That isn't a video, so it has no sound of its own | Das ist kein Video, deshalb hat es keinen eigenen Ton | Bu bir video değil, kendi sesi yok |  |
| That kind of edit isn't available for iPhone renders yet. | Diese Art von Edit gibt es für Videos auf dem iPhone noch nicht. | Bu tür bir düzenleme iPhone'da henüz kullanılamıyor. |  |
| That kind of edit isn't available for this draft yet. | Diese Art von Edit gibt es für diesen Entwurf noch nicht. | Bu tür bir düzenleme bu taslakta henüz yapılamıyor. |  |
| That kind of edit isn't available for this project yet. Should I make it as a different kind of edit? | Diese Art von Edit gibt es für dieses Projekt noch nicht. Soll ich es als andere Art von Edit machen? | Bu tür düzenleme bu proje için henüz yok. Başka türde bir düzenleme olarak yapayım mı? |  |
| That label is too long to fit on the screen whole, so I left it out instead of cutting it | Dieses Label ist zu lang, um ganz auf den Bildschirm zu passen, deshalb habe ich es weggelassen, statt es abzuschneiden | Bu etiket ekrana tam sığmayacak kadar uzun; kesmek yerine hiç koymadım |  |
| That line is said too early in that clip to come after your voice | Dieser Satz wird in dem Clip zu früh gesagt, um nach deiner Stimme zu kommen | Bu cümle o klipte seslendirmeden sonra gelemeyecek kadar erken söyleniyor |  |
| That line is too long to play on after your voice (6 seconds at most) | Dieser Satz ist zu lang, um nach deiner Stimme weiterzulaufen (höchstens 6 Sekunden) | Bu cümle seslendirmeden sonra çalmak için çok uzun (en fazla 6 saniye) |  |
| That line runs past the end of that clip, so I couldn't play it whole | Dieser Satz geht über das Ende des Clips hinaus, deshalb konnte ich ihn nicht ganz abspielen | Bu cümle o klibin sonundan taşıyor, o yüzden tam çalamadım |  |
| That moment is in the first seconds, which stay on you | Dieser Moment liegt in den ersten Sekunden, und die bleiben bei dir | O an ilk saniyelerde; o saniyeler sende kalır |  |
| That moment plays normally, without a freeze. | Dieser Moment läuft normal, ohne Standbild. | O an normal oynar, donmadan. |  |
| That part can't make a {asked_s}-second edit without changing that line. | Aus diesem Teil kann ich keinen {asked_s}-Sekunden-Edit machen, ohne diesen Satz zu ändern. | {lines_tr} değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That part can't make a {asked_s}-second edit without changing those lines. | Aus diesem Teil kann ich keinen {asked_s}-Sekunden-Edit machen, ohne diese Sätze zu ändern. | {lines_tr} değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That part runs longer than {asked_s} seconds, and I can't shorten it without changing that line. | Dieser Teil ist länger als {asked_s} Sekunden, und ich kann ihn nicht kürzen, ohne diesen Satz zu ändern. | O kısım {asked_s} saniyeden uzun ve {lines_tr} değiştirmeden kısaltamam. |  |
| That part runs longer than {asked_s} seconds, and I can't shorten it without changing those lines. | Dieser Teil ist länger als {asked_s} Sekunden, und ich kann ihn nicht kürzen, ohne diese Sätze zu ändern. | O kısım {asked_s} saniyeden uzun ve {lines_tr} değiştirmeden kısaltamam. |  |
| That part starts too close to the end to show the label | Dieser Teil beginnt zu nah am Ende, um das Label zu zeigen | O bölüm sona çok yakın başlıyor, etiketi gösteremedim |  |
| That phrase is too short to cut without removing the whole take | Diese Stelle ist zu kurz, um sie zu schneiden, ohne den ganzen Take zu entfernen | Bu ifade, çekimin tamamını kaldırmadan kesilemeyecek kadar kısa |  |
| That render didn't finish. Your approved draft is still saved, so you can retry without rebuilding the edit. | Das Video wurde nicht fertig. Dein freigegebener Entwurf ist noch gespeichert, du kannst es also nochmal versuchen, ohne den Edit neu aufzubauen. | Bu video tamamlanamadı. Onayladığın taslak hâlâ kayıtlı, düzenlemeyi baştan kurmadan tekrar deneyebilirsin. |  |
| That render didn't finish. Your confirmed plan is saved. | Das Video wurde nicht fertig. Dein bestätigter Plan ist gespeichert. | Bu video tamamlanamadı. Onayladığın plan kayıtlı. |  |
| That render has settled. I’ve refreshed the project status for you. | Dieses Video ist abgeschlossen. Ich habe den Projektstatus für dich aktualisiert. | O video işlemi sona erdi. Projenin durumunu senin için güncelledim. |  |
| That sequence cannot be safely edited in this version. | Diese Abfolge lässt sich in dieser Version nicht sicher bearbeiten. | Bu sürümde bu dizi güvenle düzenlenemiyor. |  |
| That song cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem Song kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That song cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem Song kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That spoken take cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem gesprochenen Take kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That spoken take cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem gesprochenen Take kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That voiceover with the selected footage cannot make a {asked_s}-second edit without changing the source timing. | Mit diesem Voiceover und dem ausgewählten Material kann ich keinen {asked_s}-Sekunden-Edit machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam. |  |
| That voiceover with the selected footage cannot make an edit of at most {asked_s} seconds without changing the source timing. | Mit diesem Voiceover und dem ausgewählten Material kann ich keinen Edit von höchstens {asked_s} Sekunden machen, ohne das Timing der Quelle zu ändern. | kaynağın zamanlamasını değiştirmeden en fazla {asked_s} saniyelik bir video yapamam. |  |
| That would change the wording you approved, so I left it as it is. | Das würde den Wortlaut ändern, den du freigegeben hast, deshalb habe ich es so gelassen, wie es ist. | Bu, onayladığın yazıyı değiştirirdi; o yüzden olduğu gibi bıraktım. |  |
| That would change which clips keep their sound in the edit you approved; approve the change and I'll rebuild it | Damit würde sich ändern, welche Clips in dem freigegebenen Edit ihren Ton behalten; gib die Änderung frei, dann baue ich ihn neu | Bu, onayladığın düzenlemede hangi kliplerin sesini koruduğunu değiştirirdi; değişikliği onaylarsan yeniden hazırlarım |  |
| That would change {phrase}, which you did not flag. Flag it and try again. | Damit würde {phrase} geändert, das du nicht markiert hast. Markier es und versuch es nochmal. | Bu, işaretlemediğin {phrase} bölümünü değiştirirdi. Onu da işaretleyip tekrar dene. |  |
| That's the same message I just finished, so I didn't plan it again. | Das ist dieselbe Nachricht, die ich gerade fertig bearbeitet habe, deshalb habe ich nicht nochmal geplant. | Bu, az önce bitirdiğim mesajın aynısı; o yüzden yeniden planlamadım. |  |
| That's the same message I'm already working on, so I won't start it twice. Your project is saved. | Das ist dieselbe Nachricht, an der ich schon arbeite, deshalb starte ich sie nicht doppelt. Dein Projekt ist gespeichert. | Bu, üzerinde zaten çalıştığım mesajın aynısı; o yüzden iki kez başlatmıyorum. Projen kaydedildi. |  |
| That's the same message as the draft waiting for you. Review the pinned approval before I start the render. | Das ist dieselbe Nachricht wie beim Entwurf, der auf dich wartet. Sieh dir die angeheftete Freigabe an, bevor ich das Video erstelle. | Bu, onayını bekleyen taslağın mesajıyla aynısı. Videoyu oluşturmaya başlamadan önce sabitlenen onayı incele. |  |
| That's the same message as the render in progress, so I won't start it twice. Your approved render is on its way. | Das ist dieselbe Nachricht wie beim Video, das gerade erstellt wird, deshalb starte ich es nicht doppelt. Dein freigegebenes Video ist unterwegs. | Bu, devam eden videonun mesajıyla aynısı; o yüzden iki kez başlatmıyorum. Onayladığın video hazırlanıyor. |  |
| The allowed sections changed the reviewed timing candidate. Your current draft is unchanged; review the selected sections first. | Die erlaubten Abschnitte haben den geprüften Timing-Vorschlag verändert. Dein aktueller Entwurf ist unverändert; sieh dir zuerst die ausgewählten Abschnitte an. | İzin verilen bölümler incelenen zamanlama taslağını değiştirdi. Mevcut taslağın değişmedi; önce seçili bölümleri gözden geçir. |  |
| The arrow isn't in the video yet; you can draw one with the pen in the editor | Der Pfeil ist noch nicht im Video; du kannst im Editor mit dem Stift einen zeichnen | Ok henüz videoda yok; editörde kalemle bir ok çizebilirsin |  |
| The captions are in {language_name_rendered}, not {language_name_asked}. | Die Untertitel sind auf {language_name_rendered}, nicht auf {language_name_asked}. | Altyazılar {language_name_asked} değil, {language_name_rendered} olarak çıktı. |  |
| The captions are in {language_name_rendered}, translated from the {language_name_spoken} voiceover. | Die Untertitel sind auf {language_name_rendered}, übersetzt aus dem Voiceover auf {language_name_spoken}. | Altyazılar {language_name_rendered}, {language_name_spoken} seslendirmeden çevrildi. |  |
| The captions are in {language_name_rendered}, translated from the {language_name_translated_from} speech. | Die Untertitel sind auf {language_name_rendered}, übersetzt aus der Sprache {language_name_translated_from}. | Altyazılar {language_name_rendered}, {language_name_translated_from} konuşmadan çevrildi. |  |
| The captions are in {language_name_rendered}. | Die Untertitel sind auf {language_name_rendered}. | Altyazılar {language_name_rendered}. |  |
| The captions no longer look the way you asked. | Die Untertitel sehen nicht mehr so aus, wie du es wolltest. | Altyazılar artık istediğin gibi görünmüyor. |  |
| The captions show up to {drawn_words} words at a time, not one. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht nur eines. | Altyazılar aynı anda bir değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions show up to {drawn_words} words at a time, not three. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht drei. | Altyazılar aynı anda üç değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions show up to {drawn_words} words at a time, not two. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht zwei. | Altyazılar aynı anda iki değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions show up to {drawn_words} words at a time, not {en_n}. | Die Untertitel zeigen bis zu {drawn_words} Wörter auf einmal, nicht {en_n}. | Altyazılar aynı anda {tr_n} değil, en fazla {drawn_words} kelime gösteriyor. |  |
| The captions sit higher than you asked so they stay off your face. | Die Untertitel sitzen höher als gewünscht, damit sie nicht über deinem Gesicht liegen. | Altyazılar yüzüne gelmesin diye istediğinden daha yukarıda duruyor. |  |
| The captions sit lower than you asked so they stay off your face. | Die Untertitel sitzen tiefer als gewünscht, damit sie nicht über deinem Gesicht liegen. | Altyazılar yüzüne gelmesin diye istediğinden daha aşağıda duruyor. |  |
| The captions skip that word. | Die Untertitel lassen dieses Wort aus. | Altyazılar o kelimeyi atlar. |  |
| The captions won't be held to one words at a time. | Die Untertitel werden nicht auf ein Wort auf einmal begrenzt. | Altyazılar aynı anda bir kelimeyle sınırlı kalmayacak. |  |
| The captions won't be held to three words at a time. | Die Untertitel werden nicht auf drei Wörter auf einmal begrenzt. | Altyazılar aynı anda üç kelimeyle sınırlı kalmayacak. |  |
| The captions won't be held to two words at a time. | Die Untertitel werden nicht auf zwei Wörter auf einmal begrenzt. | Altyazılar aynı anda iki kelimeyle sınırlı kalmayacak. |  |
| The captions won't be held to {en_n} words at a time. | Die Untertitel werden nicht auf {en_n} Wörter auf einmal begrenzt. | Altyazılar aynı anda {tr_n} kelimeyle sınırlı kalmayacak. |  |
| The captions write {wrong_spellings}. You can fix that in the editor. | In den Untertiteln steht {wrong_spellings}. Das kannst du im Editor korrigieren. | Altyazılarda {wrong_spellings} yazıyor. Bunu editörde düzeltebilirsin. |  |
| The clip changed since you marked {named}; mark it again | Der Clip hat sich geändert, seit du {named} markiert hast; markier es nochmal | {named_tr} işaretlediğinden beri klip değişti; tekrar işaretle |  |
| The clip the small card of {name} should follow isn't in this edit | Der Clip, dem die kleine Karte von {name} folgen soll, ist nicht in diesem Edit | {name} küçük kartının takip edeceği klip bu düzenlemede yok |  |
| The clip where you say {quote} closes the video. | Der Clip, in dem du {quote} sagst, beendet das Video. | {quote} dediğin klip videoyu kapatıyor. |  |
| The clip where you say {quote} comes right after your opening shot. | Der Clip, in dem du {quote} sagst, kommt direkt nach deiner ersten Einstellung. | {quote} dediğin klip açılış çekiminden hemen sonra geliyor. |  |
| The clip where you say {quote} comes right before your last shot. | Der Clip, in dem du {quote} sagst, kommt direkt vor deiner letzten Einstellung. | {quote} dediğin klip son çekiminden hemen önce geliyor. |  |
| The clip where you say {quote} opens the video. | Der Clip, in dem du {quote} sagst, eröffnet das Video. | {quote} dediğin klip videoyu açıyor. |  |
| The clip with your speech is too short to cut other footage into. | Der Clip mit deiner Sprache ist zu kurz, um anderes Material hineinzuschneiden. | Konuştuğun klip, araya başka çekimler koymak için çok kısa. |  |
| The clip you asked to skip is still in the video | Der Clip, den du weglassen wolltest, ist noch im Video | Çıkarmanı istediğin klip hâlâ videoda |  |
| The clips are already in that order. | Die Clips sind schon in dieser Reihenfolge. | Klipler zaten bu sırada. |  |
| The clips don't carry the exact text you gave. | In den Clips steht nicht der Text, den du angegeben hast. | Kliplerde verdiğin tam yazı yok. |  |
| The clips play in the order they were added to this project. | Die Clips laufen in der Reihenfolge, in der sie zum Projekt hinzugefügt wurden. | Klipler projeye eklendikleri sırayla oynar. |  |
| The clips' own sound is off in this video | Der eigene Ton der Clips ist in diesem Video aus | Bu videoda kliplerin kendi sesi kapalı |  |
| The clips' own sound plays, but other sound plays over it | Der eigene Ton der Clips läuft, aber ein anderer Ton läuft darüber | Kliplerin kendi sesi çalıyor ama üstüne başka bir ses de çalıyor |  |
| The confirmed edit is ready. Watch it and tell me what feels right or what you want changed. | Der bestätigte Edit ist fertig. Schau ihn dir an und sag mir, was passt oder was du ändern willst. | Onayladığın video hazır. İzle ve nasıl hissettirdiğini ya da neyi değiştirmek istediğini söyle. |  |
| The current cut is ready for your review. | Der aktuelle Schnitt ist bereit für deine Prüfung. | Şu anki kesim incelemen için hazır. |  |
| The current {current_slots}-slot edit leaves room for only {available} additional slots under the {guided_timeline_max_slots}-slot Save limit, but all {requested} ready unused sources were requested. | Im aktuellen Edit mit {current_slots} Plätzen ist unter dem Speicherlimit von {guided_timeline_max_slots} Plätzen nur noch Platz für {available} weitere, aber es wurden alle {requested} bereiten, unbenutzten Quellen angefragt. | Şu anki düzenlemede {current_slots} klip yeri dolu; {guided_timeline_max_slots} yerlik kaydetme sınırına kadar yalnızca {available} yer daha var, ama kullanılmayan {requested} hazır kaynağın hepsi istendi. |  |
| The cut doesn't hold the whole line {quote} | Der Schnitt enthält nicht den ganzen Satz {quote} | Kesit {quote} cümlesinin tamamını içermiyor |  |
| The draft already starts at {removed_trim_start:g} seconds, so I didn't change it. | Der Entwurf beginnt schon bei {removed_trim_start:g} Sekunden, deshalb habe ich nichts geändert. | Taslak zaten {removed_trim_start:g} saniyede başlıyor, o yüzden bir şey değiştirmedim. |  |
| The draft changed during the timing review. Your current preview is unchanged; review the current draft first. | Der Entwurf hat sich während der Timing-Prüfung geändert. Deine aktuelle Vorschau ist unverändert; sieh dir zuerst den aktuellen Entwurf an. | Zamanlama incelemesi sırasında taslak değişti. Mevcut önizlemen değişmedi; önce güncel taslağı gözden geçir. |  |
| The earlier story outline is saved for review and is not part of this plan. | Der frühere Story-Entwurf ist zur Ansicht gespeichert und nicht Teil dieses Plans. | Önceki hikâye taslağı inceleme için saklandı ve bu planın parçası değil. |  |
| The end screen doesn't show all of your words | Das Endbild zeigt nicht deinen ganzen Text | Son ekran yazının tamamını göstermiyor |  |
| The exported video didn't arrive intact, so it wasn't published. Your approved edit is saved. Retry the same edit. | Das exportierte Video ist nicht vollständig angekommen, deshalb wurde es nicht veröffentlicht. Dein freigegebener Edit ist gespeichert. Versuch denselben Edit nochmal. | Dışa aktarılan video eksiksiz ulaşmadığı için yayınlanmadı. Onayladığın düzenleme kayıtlı. Aynı düzenlemeyi yeniden dene. |  |
| The finished video doesn't end on that video | Das fertige Video endet nicht mit diesem Video | Biten video o videoyla bitmiyor |  |
| The finished video doesn't end on your colour screen | Das fertige Video endet nicht mit deinem Farbbild | Bitmiş video senin renkli ekranınla bitmiyor |  |
| The finished video doesn't have a photo or video behind you | Im fertigen Video ist hinter dir kein Foto oder Video | Biten videoda arkanda fotoğraf ya da video yok |  |
| The finished video doesn't have a plain colour behind you | Im fertigen Video ist hinter dir keine einfarbige Fläche | Biten videoda arkanda düz renk yok |  |
| The finished video doesn't play that clip in your talk | Das fertige Video spielt diesen Clip nicht in deiner Rede ab | Biten videoda o klip konuşmanın içinde oynamıyor |  |
| The finished video doesn't play the clip sound that way | Das fertige Video spielt den Clip-Ton nicht so ab | Bitmiş video klip sesini bu şekilde çalmıyor |  |
| The finished video ends on that video but plays muted, without its own sound | Das fertige Video endet mit diesem Video, aber stumm, ohne eigenen Ton | Biten video o videoyla bitiyor ama sessiz çalıyor, kendi sesi yok |  |
| The finished video ends on that video with its own sound, not muted | Das fertige Video endet mit diesem Video mit eigenem Ton, nicht stumm | Biten video o videoyla kendi sesiyle bitiyor, sessiz değil |  |
| The finished video ends on that video, but that video has no sound of its own | Das fertige Video endet mit diesem Video, aber das hat keinen eigenen Ton | Biten video o videoyla bitiyor ama o videonun kendi sesi yok |  |
| The finished video is {round_s_measured}s including its ending; you asked for under {round_s_maximum}s. | Das fertige Video ist mit Abschluss {round_s_measured} s lang; du wolltest unter {round_s_maximum} s. | Kapanışı dahil bitmiş video {round_s_measured} saniye; {round_s_maximum} saniyenin altında istemiştin. |  |
| The finished video plays this clip at normal speed | Das fertige Video spielt diesen Clip in normaler Geschwindigkeit ab | Bitmiş video bu klibi normal hızda oynatıyor |  |
| The finished video plays {shown} s of your {total} s clip, so it's cut. | Das fertige Video spielt {shown} s von deinem {total} s langen Clip ab, er ist also gekürzt. | Bitmiş video {total} sn'lik klibinin yalnızca {shown} sn'sini oynatıyor, yani klip kesiliyor. |  |
| The fix is in the new video, but I couldn't confirm it worked. You can still undo it. | Die Korrektur ist im neuen Video, aber ich konnte nicht bestätigen, dass sie geklappt hat. Du kannst sie weiterhin rückgängig machen. | Düzeltme yeni videoda, ama işe yaradığını doğrulayamadım. İstersen hâlâ geri alabilirsin. |  |
| The fix made the video worse, so I undid it in your draft. The video still shows the fixed version. Ask me to render your previous version if you want it. | Die Korrektur hat das Video verschlechtert, deshalb habe ich sie in deinem Entwurf rückgängig gemacht. Das Video zeigt noch die korrigierte Version. Sag mir, wenn ich deine vorherige Version erstellen soll. | Düzeltme videoyu kötüleştirdi, bu yüzden taslağında geri aldım. Video hâlâ düzeltilmiş hâliyle duruyor. İstersen önceki sürümü oluşturmamı söyle. |  |
| The fix may have made the video worse. Your newer edits are untouched. | Die Korrektur hat das Video vielleicht verschlechtert. Deine neueren Änderungen habe ich nicht angefasst. | Düzeltme videoyu kötüleştirmiş olabilir. Yeni düzenlemelerine dokunmadım. |  |
| The footage sound plays at about {pct}% alongside your voice, not far under it | Der Originalton läuft mit etwa {pct} % neben deiner Stimme, nicht weit darunter | Çekim sesi sesinin yanında yaklaşık %{pct} seviyesinde çalıyor, pek altında değil |  |
| The footage sound plays at full volume alongside your voice | Der Originalton läuft in voller Lautstärke neben deiner Stimme | Çekim sesi sesinle aynı seviyede, tam sesle çalıyor |  |
| The footage sound still plays | Der Originalton läuft noch | Çekim sesi hâlâ çalıyor |  |
| The footage sound still plays at full volume alongside your voice; {fix} | Der Originalton läuft noch in voller Lautstärke neben deiner Stimme; {fix} | {v1}, sesinle aynı seviyede; {fix} |  |
| The footage sound still plays softly under your voice (about {pct}%); {fix} | Der Originalton läuft noch leise unter deiner Stimme (etwa {pct} %); {fix} | {v1}, sesinin altında kısık{about_tr}; {fix} |  |
| The footage sound still plays softly under your voice; {fix} | Der Originalton läuft noch leise unter deiner Stimme; {fix} | {v1}, sesinin altında kısık{about_tr}; {fix} |  |
| The full-screen photo followed by its video has not been verified | Das Vollbild-Foto mit dem anschließenden Video wurde nicht geprüft | Tam ekran fotoğrafın ardından videonun geldiği doğrulanmadı |  |
| The label on the video reads {shown}, not your exact words | Das Label im Video lautet {shown}, nicht genau deine Worte | Videodaki etiket {shown} yazıyor, tam senin yazdığın gibi değil |  |
| The labels are place names, not the hour each clip was filmed. | Die Labels sind Ortsnamen, nicht die Uhrzeit, zu der der Clip gefilmt wurde. | Etiketler yer adı, klibin çekildiği saat değil. |  |
| The labels you edited by hand were kept, and no other clip needs a label. | Die Labels, die du von Hand bearbeitet hast, sind geblieben, und kein anderer Clip braucht ein Label. | Elle düzenlediğin etiketleri korudum, başka bir klibe etiket gerekmiyor. |  |
| The line “{quote}” is longer than the video can hold with your other moments, so the video won't end exactly on it. | Der Satz „{quote}“ ist mit deinen anderen Momenten zu lang fürs Video, deshalb endet das Video nicht genau damit. | “{quote}” cümlesi diğer anlarla birlikte videoya sığmıyor, bu yüzden video tam o anda bitmeyecek. |  |
| The longest moment runs {longest:.1f} s | Der längste Moment dauert {longest:.1f} s | En uzun an {longest:.1f} sn sürüyor |  |
| The longest moment runs {longest:.1f} s, not about {ask_each_s:g} s | Der längste Moment dauert {longest:.1f} s, nicht etwa {ask_each_s:g} s | En uzun an {longest:.1f} sn sürüyor, yaklaşık {ask_each_s:g} sn değil |  |
| The new order has to list every clip in the edit exactly once. | Die neue Reihenfolge muss jeden Clip im Edit genau einmal enthalten. | Yeni sıra, videodaki her klibi tam bir kez içermeli. |  |
| The part of clip {number} in this video doesn't show that moment, so it plays at normal speed | Der Teil von Clip {number} in diesem Video zeigt diesen Moment nicht, deshalb läuft er in normaler Geschwindigkeit | Videodaki {number}. klip parçası o anı göstermiyor, bu yüzden normal hızda oynuyor |  |
| The part where "{line}" is said isn't in the edit | Die Stelle, an der "{line}" gesagt wird, ist nicht im Edit | "{line}" denen kısım düzenlemede yok |  |
| The phone render keeps the text off what you named and checks it. | Das Video auf dem Handy hält den Text von dem fern, was du genannt hast, und prüft das. | Telefon oluşturması yazıyı belirttiğin yerlerden uzak tutup kontrol ediyor. |  |
| The photo or video behind you is only the background, not also a pop-in on a spoken word. | Das Foto oder Video hinter dir ist nur der Hintergrund und erscheint nicht zusätzlich als Einblendung bei einem gesprochenen Wort. | Arkandaki fotoğraf ya da video sadece arka plan, söylediğin bir kelimede ayrıca çıkmıyor. |  |
| The photos you timed play at the edit's own pace so everything fits. | Die Fotos, bei denen du eine Zeit angegeben hast, laufen im eigenen Tempo des Edits, damit alles passt. | Süre verdiğin fotoğraflar her şey sığsın diye düzenlemenin kendi hızında oynar. |  |
| The plain colour behind you is {fact_color}, not the {ask} you asked for. | Die einfarbige Fläche hinter dir ist {fact_color}, nicht {ask} wie gewünscht. | Arkandaki düz renk {fact_color}; istediğin {ask} değil. |  |
| The plain colour stays behind you the whole time; no photos or videos are attached to take turns over it. Attach them in Visuals | Die einfarbige Fläche bleibt die ganze Zeit hinter dir; es sind keine Fotos oder Videos angehängt, die sich darüber abwechseln. Hänge sie in Visuals an | Düz renk bütün video boyunca arkanda kalıyor; üstünde sırayla değişecek fotoğraf ya da video ekli değil. Onları Visuals'a ekle |  |
| The plain colour stays behind you the whole time; nothing changes over it | Die einfarbige Fläche bleibt die ganze Zeit hinter dir; darüber ändert sich nichts | Düz renk bütün video boyunca arkanda kalıyor; üstünde hiçbir şey değişmiyor |  |
| The plain colour stays behind you the whole time; your iPhone can't switch backgrounds yet | Die einfarbige Fläche bleibt die ganze Zeit hinter dir; dein iPhone kann Hintergründe noch nicht wechseln | Düz renk bütün video boyunca arkanda kalıyor; iPhone'un henüz arka planı değiştiremiyor |  |
| The planned explanations need more reading time than this edit allows. Should we use a longer edit or explain fewer parts? | Die geplanten Erklärungen brauchen mehr Lesezeit, als dieser Edit hergibt. Sollen wir einen längeren Edit nehmen oder weniger Teile erklären? | Planlanan açıklamaları okumak için bu videoda yeterli süre yok. Videoyu uzatalım mı, yoksa daha az bölümü mü açıklayalım? |  |
| The planned story structure ({lines_listed_items}) sets the chapters (which clips are grouped together), but it won't reorder your clips: they stay in the order you gave | Die geplante Story-Struktur ({lines_listed_items}) legt die Kapitel fest (welche Clips zusammengehören), ordnet deine Clips aber nicht um: Sie bleiben in der Reihenfolge, die du vorgegeben hast | Planlanan hikâye yapısı ({lines_listed_items}) bölümleri (hangi kliplerin birlikte gruplanacağını) belirliyor ama kliplerinin sırasını değiştirmeyecek: verdiğin sırada kalacaklar |  |
| The planned story structure ({list_items}) sets the chapters (which clips are grouped together), but it won't reorder your clips: they stay in the order you gave | Die geplante Story-Struktur ({list_items}) legt die Kapitel fest (welche Clips zusammengehören), ordnet deine Clips aber nicht um: Sie bleiben in der Reihenfolge, die du vorgegeben hast | Planlanan hikâye yapısı ({list_items}) bölümleri (hangi kliplerin birlikte gruplanacağını) belirliyor ama kliplerinin sırasını değiştirmeyecek: verdiğin sırada kalacaklar |  |
| The post caption can't be changed from here yet. | Der Beitragstext lässt sich von hier aus noch nicht ändern. | Paylaşım metni henüz buradan değiştirilemiyor. |  |
| The previous render's outcome is still unknown. I kept your corrected draft and did not start another render. Refresh the project to check its status. | Das Ergebnis des vorherigen Videos ist noch unbekannt. Ich habe deinen korrigierten Entwurf behalten und kein weiteres Video gestartet. Lade das Projekt neu, um den Status zu prüfen. | Önceki videonun sonucu hâlâ bilinmiyor. Düzeltilmiş taslağını sakladım ve başka bir video başlatmadım. Sonucu kontrol etmek için projeyi yenile. |  |
| The previous render's outcome is still unknown. Refresh the project to check it before starting another render. Your saved draft is unchanged. | Das Ergebnis des vorherigen Videos ist noch unbekannt. Lade das Projekt neu und prüfe es, bevor du ein weiteres Video startest. Dein gespeicherter Entwurf ist unverändert. | Önceki videonun sonucu hâlâ bilinmiyor. Başka bir video başlatmadan önce sonucu kontrol etmek için projeyi yenile. Kayıtlı taslağın değişmedi. |  |
| The project changed before I could start that render. I kept your draft; ask me to prepare it again. | Das Projekt hat sich geändert, bevor ich das Video starten konnte. Ich habe deinen Entwurf behalten; sag mir, dass ich es nochmal vorbereiten soll. | Videoyu başlatamadan önce proje değişti. Taslağını sakladım; tekrar hazırlamamı isteyebilirsin. |  |
| The project ownership changed during the timing review. | Der Besitzer des Projekts hat sich während der Timing-Prüfung geändert. | Zamanlama incelemesi sırasında projenin sahipliği değişti. |  |
| The proposed edit isn't supported by the current renderer or footage. | Die vorgeschlagene Änderung wird vom aktuellen Renderer oder Material nicht unterstützt. | Önerilen düzenleme mevcut oluşturucu veya çekimlerle desteklenmiyor. |  |
| The proposed text differs from the wording you approved. Let's review it again. | Der vorgeschlagene Text weicht von dem ab, den du freigegeben hast. Sehen wir ihn uns nochmal an. | Önerilen yazı onayladığın yazıdan farklı. Tekrar gözden geçirelim. |  |
| The recording for {name} never shows. | Die Aufnahme für {name} erscheint nie. | {name} kaydı hiç görünmüyor. |  |
| The recordings for {names} never show. | Die Aufnahmen für {names} erscheinen nie. | {names} kayıtları hiç görünmüyor. |  |
| The render couldn't be handed to the queue. Give it another go. | Das Video konnte nicht an die Warteschlange übergeben werden. Versuch es nochmal. | Video sıraya iletilemedi. Bir kez daha dene. |  |
| The render queue did not confirm whether it received this edit. Your approved draft is saved; I won't send it twice until it is reconciled. | Die Warteschlange hat nicht bestätigt, ob sie diesen Edit erhalten hat. Dein freigegebener Entwurf ist gespeichert; ich schicke ihn nicht doppelt, bis das geklärt ist. | Video sırası bu düzenlemeyi alıp almadığını doğrulamadı. Onayladığın taslak kayıtlı; durum netleşene kadar aynı işi ikinci kez göndermeyeceğim. |  |
| The render stopped before a complete render plan was saved. Your approved direction and footage are safe. Ask for a new version and review its approval. | Das Video wurde gestoppt, bevor ein vollständiger Plan gespeichert war. Deine freigegebene Richtung und dein Material sind sicher. Bitte um eine neue Version und prüfe ihre Freigabe. | Video, eksiksiz oluşturma planı kaydedilmeden durdu. Onayladığın yön ve görüntüler güvende. Yeni bir sürüm isteyip onayını gözden geçir. |  |
| The requested licensed sound effect is unavailable. Choose another effect. | Der gewünschte lizenzierte Soundeffekt ist nicht verfügbar. Wähl einen anderen Effekt. | İstediğin lisanslı ses efekti yok. Başka bir efekt seç. |  |
| The requested licensed sound effect {name} is unavailable. | Der gewünschte lizenzierte Soundeffekt {name} ist nicht verfügbar. | İstediğin lisanslı ses efekti ({name}) yok. |  |
| The requested {requested:g} seconds cannot include these clips with their approved holds. Choose fewer clips or change those holds before rendering. | Die gewünschten {requested:g} Sekunden reichen nicht für diese Clips mit ihren freigegebenen Haltezeiten. Wähl weniger Clips oder ändere diese Haltezeiten, bevor ich das Video erstelle. | İstenen {requested:g} saniye, onaylı bekleme süreleriyle bu klipleri kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme sürelerini değiştir. |  |
| The reviewed story's source windows, wording and sound match its frozen execution. | Die Quellabschnitte, der Wortlaut und der Ton der geprüften Story passen zur festgelegten Umsetzung. | Gözden geçirilen hikâyenin kaynak aralıkları, metni ve sesi sabitlenen düzenle eşleşiyor. |  |
| The same photo is down for both "{asked}" and "{other}". Which clip is "{asked}"? Tap it, or tell me there isn't one. | Dasselbe Foto ist für "{asked}" und "{other}" eingetragen. Welcher Clip ist "{asked}"? Tipp ihn an oder sag mir, dass es keinen gibt. | "{asked}" ve "{other}" için aynı fotoğraf seçilmiş. "{asked}" hangi klip? Ona dokun ya da olmadığını söyle. |  |
| The same photo is down for more than one of your moments. Tap the clip for each of {shown}{more}, or mark the ones that have none. | Dasselbe Foto ist für mehrere deiner Momente eingetragen. Tipp für jeden von {shown}{more} den Clip an oder markier die, die keinen haben. | Birden fazla anın için aynı fotoğraf seçilmiş. Şunlar için klibi seç: {shown}{more}. Klibi olmayanları işaretle. |  |
| The same video is in the edit more than once: {where} | Dasselbe Video ist mehrfach im Edit: {where} | Aynı video düzenlemede birden fazla kez var: {where} |  |
| The saved edit's footage changed. Ask me to prepare it again. | Das Material des gespeicherten Edits hat sich geändert. Sag mir, dass ich ihn nochmal vorbereiten soll. | Kayıtlı düzenlemenin çekimleri değişti. Yeniden hazırlamamı iste. |  |
| The saved edit's rendering evidence changed. Ask me to prepare it again. | Die Render-Nachweise des gespeicherten Edits haben sich geändert. Sag mir, dass ich ihn nochmal vorbereiten soll. | Kayıtlı düzenlemenin oluşturma kanıtları değişti. Yeniden hazırlamamı iste. |  |
| The selected clips add up to less than 3 seconds without reuse. Add footage so they reach at least 3 seconds. | Die ausgewählten Clips ergeben ohne Wiederholung weniger als 3 Sekunden. Füg Material hinzu, damit sie mindestens 3 Sekunden erreichen. | Seçili klipler tekrar olmadan en az 3 saniye sağlayabilsin diye çekim ekle. |  |
| The selected footage cannot meet the included-clip constraints. | Das ausgewählte Material kann die Vorgaben für die enthaltenen Clips nicht erfüllen. | Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor. |  |
| The selected footage runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter footage. | Das ausgewählte Material dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm kürzeres Material. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected footage runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Das ausgewählte Material dauert {natural_duration:g} Sekunden, reicht ohne Wiederholung aber nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected idea's execution or sources changed. Review the idea again. | Die Umsetzung oder die Quellen der ausgewählten Idee haben sich geändert. Sieh dir die Idee nochmal an. | Seçilen fikrin düzeni veya kaynakları değişti. Fikri yeniden gözden geçir. |  |
| The selected order conflicts with the single-hero composition. Choose the hero first, followed by its supporting clips. | Die gewählte Reihenfolge passt nicht zum Aufbau mit einem Hauptclip. Wähl zuerst den Hauptclip, danach die Clips, die ihn unterstützen. | Seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, ardından destekleyen klipleri seç. |  |
| The selected song runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter song. | Der ausgewählte Song dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm einen kürzeren Song. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected song runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Der ausgewählte Song dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected take runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter take. | Der ausgewählte Take dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm einen kürzeren Take. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected take runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Der ausgewählte Take dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected voiceover runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter voiceover. | Das ausgewählte Voiceover dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm ein kürzeres Voiceover. | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected voiceover runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Das ausgewählte Voiceover dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The selected {natural_owner} runs {natural_duration:g} seconds, but an edit can be at most {edit_cap:g} seconds. Use a shorter {natural_owner}. | Die ausgewählte Quelle ({natural_owner}) dauert {natural_duration:g} Sekunden, aber ein Edit darf höchstens {edit_cap:g} Sekunden lang sein. Nimm eine kürzere Quelle ({natural_owner}). | Seçili kaynak {natural_duration:g} saniye sürüyor ama bir video en fazla {edit_cap:g} saniye olabilir. Daha kısa bir kaynak kullan. |  |
| The selected {natural_owner} runs {natural_duration:g} seconds, but the footage can provide only {raw_duration:g} seconds without reuse. Add longer footage. | Die ausgewählte Quelle ({natural_owner}) dauert {natural_duration:g} Sekunden, aber das Material reicht ohne Wiederholung nur für {raw_duration:g} Sekunden. Füg längeres Material hinzu. | Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle. |  |
| The sentence it belongs to is the last thing you say, so there is nowhere to pause after it | Der Satz, zu dem es gehört, ist das Letzte, was du sagst, deshalb gibt es danach keine Stelle für eine Pause | Ait olduğu cümle söylediğin son şey, arkasından durabileceğim bir yer yok |  |
| The separate parts of your brief produced conflicting plans. | Die einzelnen Teile deiner Anfrage haben widersprüchliche Pläne ergeben. | İsteğinin ayrı bölümleri birbiriyle çelişen planlar çıkardı. |  |
| The shortest length where every clip stays on screen long enough to be seen. | Die kürzeste Länge, bei der jeder Clip lang genug zu sehen ist. | Her klibin görülecek kadar ekranda kaldığı en kısa süre. |  |
| The simpler version leaves out: {listed}. | Die einfachere Version lässt weg: {listed}. | Daha basit sürümde çıkarılacak: {listed}. |  |
| The small card of {name} couldn't be drawn on this render | Die kleine Karte von {name} konnte in diesem Video nicht gezeichnet werden | {name} küçük kartı bu videoya çizilemedi |  |
| The small card of {name} would overlap another card, so it isn't shown | Die kleine Karte von {name} würde eine andere Karte überlappen, deshalb wird sie nicht gezeigt | {name} küçük kartı başka bir kartla çakışıyor, o yüzden gösterilmiyor |  |
| The speaker's face moves too far for one vertical crop to hold it | Das Gesicht der sprechenden Person bewegt sich zu weit für einen einzigen Hochformat-Ausschnitt | Konuşan kişinin yüzü tek bir dikey kadraja sığmayacak kadar hareket ediyor |  |
| The speech check changed before I could start. Approve again to choose how to handle the pauses. | Die Sprachprüfung hat sich geändert, bevor ich starten konnte. Gib nochmal frei, um zu wählen, wie ich mit den Pausen umgehe. | Konuşma kontrolü ben başlamadan önce değişti. Duraklamaları nasıl ele alacağımı seçmek için tekrar onayla. |  |
| The speech check changed before I could start. Tap Refresh project and I'll set it up again so you can choose how to handle the pauses. | Die Sprachprüfung hat sich geändert, bevor ich starten konnte. Tipp auf „Refresh project“, dann richte ich sie nochmal ein und du kannst wählen, wie ich mit den Pausen umgehe. | Başlamadan önce konuşma kontrolü değişti. "Refresh project" düğmesine dokun, duraklamaları nasıl ele alacağını seçebilmen için yeniden hazırlayayım. |  |
| The speech check didn't finish. Approve again to render without it, or ask me to check again. | Die Sprachprüfung wurde nicht fertig. Gib nochmal frei, um das Video ohne sie zu erstellen, oder bitte mich, nochmal zu prüfen. | Konuşma kontrolü tamamlanamadı. Onsuz videoyu oluşturmak için tekrar onayla ya da kontrolü yeniden yapmamı iste. |  |
| The speech check is still running. I'll ask again once it finishes. | Die Sprachprüfung läuft noch. Ich frage nochmal, sobald sie fertig ist. | Konuşma kontrolü hâlâ sürüyor. Bitince sana tekrar soracağım. |  |
| The speech check isn't available for this video yet. Tap Refresh project and I'll render it without cleanup. | Die Sprachprüfung gibt es für dieses Video noch nicht. Tipp auf „Refresh project“, dann erstelle ich das Video ohne Bereinigung. | Bu video için konuşma kontrolü henüz kullanılamıyor. "Refresh project" düğmesine dokun, videoyu temizleme olmadan hazırlayayım. |  |
| The text couldn't be added | Der Text konnte nicht hinzugefügt werden | Yazılar eklenemedi |  |
| The text is straight in the finished video, not curved | Der Text ist im fertigen Video gerade, nicht gebogen | Bitmiş videoda yazı kavisli değil, düz |  |
| The text on screen doesn't match what you asked for. Want me to change it to {quoted}? | Der Text auf dem Bildschirm passt nicht zu deiner Anfrage. Soll ich ihn auf {quoted} ändern? | Ekrandaki yazı istediğinle uyuşmuyor. {quoted} olarak değiştireyim mi? |  |
| The text on screen doesn't match what you asked for. Want me to change it? | Der Text auf dem Bildschirm passt nicht zu deiner Anfrage. Soll ich ihn ändern? | Ekrandaki yazı istediğinle uyuşmuyor. Değiştireyim mi? |  |
| The text {quoted} isn't in that edit. | Der Text {quoted} ist nicht in diesem Edit. | {quoted} yazısı bu düzenlemede yok. |  |
| The title lands on a face; I couldn't find a spot clear of it | Der Titel liegt auf einem Gesicht; ich habe keine freie Stelle gefunden | Başlık bir yüzün üstüne denk geliyor; yüzü açıkta bırakan bir yer bulamadım |  |
| The video changed on your iPhone after I drafted that edit, so I left it as it was. Ask me again and I'll redo the change on the current version. | Das Video hat sich auf deinem iPhone geändert, nachdem ich diese Änderung vorbereitet hatte, deshalb habe ich es so gelassen. Frag mich nochmal, dann mache ich die Änderung an der aktuellen Version. | Taslağı hazırladığımdan beri iPhone'undaki video değişti, o yüzden olduğu gibi bıraktım. Tekrar iste, değişikliği güncel sürümde yapayım. |  |
| The video doesn't end on “{quote}”: the closing cut doesn't hold that line | Das Video endet nicht mit „{quote}“: Der letzte Schnitt enthält diesen Satz nicht | Video “{quote}” cümlesiyle bitmiyor: son kesit o cümleyi içermiyor |  |
| The video ends on that clip's whole spoken line, in its own sound | Das Video endet mit dem ganzen gesprochenen Satz dieses Clips, mit seinem eigenen Ton | Video, o klipteki cümlenin tamamıyla ve klibin kendi sesiyle bitiyor |  |
| The video ends on that clip, but I couldn't confirm its whole spoken line plays in its own sound | Das Video endet mit diesem Clip, aber ich konnte nicht bestätigen, dass sein ganzer gesprochener Satz mit eigenem Ton läuft | Video o klipte bitiyor ama cümlenin tamamının klibin kendi sesiyle duyulduğunu doğrulayamadım |  |
| The video ends on that clip, but the clips' own sound is off | Das Video endet mit diesem Clip, aber der eigene Ton der Clips ist aus | Video o klipte bitiyor ama kliplerin kendi sesi kapalı |  |
| The video ends on “{quote}”, in its own sound | Das Video endet mit „{quote}“, mit eigenem Ton | Video “{quote}” cümlesiyle, klibin kendi sesiyle bitiyor |  |
| The video fills the vertical frame and follows the speaker's face, but their face isn't in view for about {absent:.0f} s of the clip | Das Video füllt das Hochformat und folgt dem Gesicht der sprechenden Person, aber das Gesicht ist etwa {absent:.0f} s des Clips nicht im Bild | Video dikey ekranı dolduruyor ve konuşan kişinin yüzünü takip ediyor, ama klibin yaklaşık {absent:.0f} sn'sinde konuşan kişinin yüzü görünmüyor |  |
| The video fills the vertical frame and the speaker's face stays in it throughout | Das Video füllt das Hochformat und das Gesicht der sprechenden Person bleibt die ganze Zeit im Bild | Video dikey ekranı dolduruyor ve konuşan kişinin yüzü baştan sona kadrajda kalıyor |  |
| The video has {len_lengths} moment, not {ask_count} | Das Video hat {len_lengths} Moment, nicht {ask_count} | Videoda {ask_count} değil {len_lengths} an var |  |
| The video has {len_lengths} moments, not {ask_count} | Das Video hat {len_lengths} Momente, nicht {ask_count} | Videoda {ask_count} değil {len_lengths} an var |  |
| The video is exactly as long as your voice. | Das Video ist genau so lang wie deine Stimme. | Video tam olarak sesin kadar uzun olur. |  |
| The video is made without an opening title. | Das Video wird ohne Titel am Anfang erstellt. | Video açılış başlığı olmadan hazırlanır. |  |
| The video keeps the clips' own sound, with nothing laid over it | Das Video behält den eigenen Ton der Clips, ohne dass etwas darübergelegt wird | Video kliplerin kendi sesini koruyor, üstüne başka ses eklenmedi |  |
| The video plays just the part your lines keep, without reuse or a silent extension. | Das Video spielt nur den Teil ab, den deine Sätze behalten, ohne Wiederholung oder stumme Verlängerung. | Video yalnızca cümlelerinin tuttuğu kısmı oynatır; tekrar ya da sessiz uzatma yok. |  |
| The video still has a photo in it | Im Video ist noch ein Foto | Videoda hâlâ bir fotoğraf var |  |
| The video still opens at the start of your take. | Das Video beginnt weiterhin am Anfang deines Takes. | Video yine çekimin başından açılır. |  |
| The video still runs to the end of your take. | Das Video läuft weiterhin bis zum Ende deines Takes. | Video yine çekimin sonuna kadar sürer. |  |
| The video would be longer than the 30 minutes your iPhone can build | Das Video wäre länger als die 30 Minuten, die dein iPhone erstellen kann | Video, iPhone'unun hazırlayabildiği 30 dakikadan uzun olurdu |  |
| The video you wanted at the end plays only there, not also on a spoken word. | Das Video, das du am Ende wolltest, läuft nur dort und nicht zusätzlich bei einem gesprochenen Wort. | Sonda istediğin video sadece orada oynuyor, söylediğin bir kelimede ayrıca çıkmıyor. |  |
| The video's shape can't be changed from chat on this edit. | Das Format des Videos kann in diesem Edit nicht per Chat geändert werden. | Bu düzenlemede videonun biçimini sohbetten değiştiremiyorum. |  |
| The word order is unavailable. Which sequence should I use? | Die Wortreihenfolge ist nicht verfügbar. Welche Abfolge soll ich nehmen? | Kelime sırası bulunamadı. Hangi diziyi kullanayım? |  |
| The {str_value_replace} direction isn't applied on this video yet: it is cut as a fast montage | Die Richtung „{str_value_replace}“ wird bei diesem Video noch nicht umgesetzt: Es wird als schnelle Montage geschnitten | {str_value_replace} yönü bu videoda henüz uygulanmıyor: hızlı bir montaj olarak kesiliyor |  |
| The {str_value_replace} pacing isn't applied on this video yet: the cuts follow a fast pace | Das Tempo „{str_value_replace}“ wird bei diesem Video noch nicht umgesetzt: Die Schnitte folgen einem schnellen Tempo | {str_value_replace} tempo bu videoda henüz uygulanmıyor: kesimler hızlı tempoda |  |
| The {variant_id_replace} cut is ready. The approved render finished; review the opening, pacing, and text, then tell me what you want changed. | {variant_id_replace} ist fertig. Das freigegebene Video ist erstellt; sieh dir den Anfang, das Tempo und die Texte an und sag mir dann, was du ändern willst. | {v1} hazır. Onayladığın video tamamlandı; açılışı, temposunu ve yazıları incele, sonra neyi değiştirmek istediğini söyle. |  |
| The {words_value} direction isn't applied on this video yet: {instead_en} | Die Richtung „{words_value}“ wird bei diesem Video noch nicht umgesetzt: {instead_en} | {words_value} yönü bu videoda henüz uygulanmıyor: {instead_tr} |  |
| The {words_value} pacing isn't applied on this video yet: {instead_en} | Das Tempo „{words_value}“ wird bei diesem Video noch nicht umgesetzt: {instead_en} | {words_value} tempo bu videoda henüz uygulanmıyor: {instead_tr} |  |
| The {word} you picked was cut out of your video, so {text} isn't shown | {word}, das du gewählt hast, wurde aus deinem Video herausgeschnitten, deshalb wird {text} nicht gezeigt | Seçtiğin {word} anı videodan kesildi, o yüzden {text} yazısı çıkmadı |  |
| There are no fast cuts between your lines | Zwischen deinen Sätzen gibt es keine schnellen Schnitte | Cümlelerin arasında hızlı kesimler yok |  |
| There are too many clips for each to appear for at least 0.1 seconds within 120 seconds. Choose fewer clips. | Es sind zu viele Clips, als dass jeder innerhalb von 120 Sekunden mindestens 0,1 Sekunden zu sehen wäre. Wähl weniger Clips. | Her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç. |  |
| There is no full-screen opening of you to take away. | Es gibt keinen Vollbild-Anfang von dir, den ich entfernen könnte. | Kaldırılacak tam ekran bir açılışın yok. |  |
| There is no photo or video to put behind you. Attach the photos or videos first, in Visuals | Es gibt kein Foto oder Video, das hinter dich gelegt werden kann. Hänge die Fotos oder Videos zuerst in Visuals an | Arkana koyacak fotoğraf ya da video yok. Önce fotoğrafları ya da videoları Visuals'tan ekle |  |
| There is nothing said in your talk to place it after | In deiner Rede wird nichts gesagt, wonach ich es platzieren könnte | Konuşmanda onu arkasına koyabileceğim bir cümle yok |  |
| There isn't a box behind the captions. | Hinter den Untertiteln ist kein Kasten. | Altyazıların arkasında kutu yok. |  |
| There isn't a current editable draft for this failed render. Your saved request is unchanged. No new render was started. Ask for a new version using your saved request. | Für dieses fehlgeschlagene Video gibt es keinen aktuellen, bearbeitbaren Entwurf. Deine gespeicherte Anfrage ist unverändert. Es wurde kein neues Video gestartet. Bitte mit deiner gespeicherten Anfrage um eine neue Version. | Bu başarısız video için güncel, düzenlenebilir bir taslak yok. Kayıtlı isteğin değişmedi. Yeni bir video başlatılmadı. Kayıtlı isteğinden yeni bir sürüm iste. |  |
| There was no room on screen for {names_no_room} without covering a face or the captions | Auf dem Bildschirm war kein Platz für {names_no_room}, ohne ein Gesicht oder die Untertitel zu verdecken | {names_no_room} için bir yüzü ya da altyazıları kapatmadan ekranda yer yoktu |  |
| There wasn't room above your title for "{trigger}" without covering your captions | Über deinem Titel war kein Platz für "{trigger}", ohne deine Untertitel zu verdecken | Altyazıları kapatmadan başlığın üstünde "{trigger}" için yer yoktu |  |
| There wasn't room above your title for "{trigger}" without covering your face | Über deinem Titel war kein Platz für "{trigger}", ohne dein Gesicht zu verdecken | Yüzünü kapatmadan başlığın üstünde "{trigger}" için yer yoktu |  |
| There were no Visuals to show as cards. | Es gab keine Visuals, die als Karten gezeigt werden konnten. | Kart olarak gösterilecek görsel yoktu. |  |
| There's background sound in some of your takes, so I only cut the long pauses in those | In einigen deiner Takes sind Hintergrundgeräusche, deshalb habe ich dort nur die langen Pausen geschnitten | Çekimlerinin bazılarında arka plan sesi var, o yüzden onlarda yalnızca uzun duraklamaları kestim |  |
| There's background sound in your take, so I only cut the long pauses | In deinem Take sind Hintergrundgeräusche, deshalb habe ich nur die langen Pausen geschnitten | Çekiminde arka plan sesi var, o yüzden yalnızca uzun duraklamaları kestim |  |
| There's no opening title to put "{trigger}" above | Es gibt keinen Titel am Anfang, über den ich "{trigger}" setzen könnte | "{trigger}" görselini koyacak bir açılış başlığı yok |  |
| There’s no edit running right now. Your latest project state is saved. | Gerade läuft kein Edit. Dein letzter Projektstand ist gespeichert. | Şu anda devam eden bir düzenleme yok. Projenin son hâli kaydedildi. |  |
| These words need at least {exc_minimum_duration_s:g}s of reading time. May I use a longer video, or would you like to shorten the text? Nothing was changed. | Diese Wörter brauchen mindestens {exc_minimum_duration_s:g} s Lesezeit. Darf ich ein längeres Video nehmen, oder möchtest du den Text kürzen? Es wurde nichts geändert. | Bu yazılar en az {exc_minimum_duration_s:g} saniye okuma süresi gerektiriyor. Daha uzun bir video kullanayım mı, yoksa metni kısaltmak ister misin? Hiçbir şey değişmedi. |  |
| This approval expired. Ask Kria to prepare it again. | Diese Freigabe ist abgelaufen. Bitte Kria, sie nochmal vorzubereiten. | Bu onayın süresi doldu. Kria'dan yeniden hazırlamasını iste. |  |
| This clip is too long for talking-to-camera on your iPhone. Use a shorter clip (under 5 minutes) and try again. No fallback edit was rendered. | Dieser Clip ist zu lang für ein Video auf deinem iPhone, in dem du in die Kamera sprichst. Nimm einen kürzeren Clip (unter 5 Minuten) und versuch es nochmal. Es wurde kein Ersatz-Edit erstellt. | Bu klip, iPhone'unda kameraya konuşma videosu için çok uzun. 5 dakikadan kısa bir klip kullan ve tekrar dene. Yedek bir düzenleme oluşturulmadı. |  |
| This day vlog edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Tagesvlog-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (günlük vlog) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This draft doesn't end on that video | Dieser Entwurf endet nicht mit diesem Video | Bu taslak o videoyla bitmiyor |  |
| This draft doesn't end on the photo you asked for | Dieser Entwurf endet nicht mit dem Foto, das du wolltest | Bu taslak istediğin fotoğrafla bitmiyor |  |
| This draft doesn't end the way you asked | Dieser Entwurf endet nicht so, wie du es wolltest | Bu taslak istediğin gibi bitmiyor |  |
| This draft doesn't place a small card at that moment. | Dieser Entwurf setzt in diesem Moment keine kleine Karte. | Bu taslak o anda küçük bir kart koymuyor. |  |
| This draft doesn't play that clip in your talk | Dieser Entwurf spielt diesen Clip nicht in deiner Rede ab | Bu taslakta o klip konuşmanın içinde oynamıyor |  |
| This draft doesn't put a photo or video behind you | Dieser Entwurf legt kein Foto oder Video hinter dich | Bu taslak arkana fotoğraf ya da video koymuyor |  |
| This draft doesn't put a plain colour behind you | Dieser Entwurf legt keine einfarbige Fläche hinter dich | Bu taslak arkana düz renk koymuyor |  |
| This draft doesn't put your picture {name} above your title. | Dieser Entwurf setzt dein Bild {name} nicht über deinen Titel. | Bu taslak {name} görselini başlığının üstüne koymuyor. |  |
| This draft doesn't use your song. | Dieser Entwurf nutzt deinen Song nicht. | Bu taslak şarkını kullanmıyor. |  |
| This draft ends on that video but muted, not with its own sound | Dieser Entwurf endet mit diesem Video, aber stumm, nicht mit eigenem Ton | Bu taslak o videoyla bitiyor ama sessiz, kendi sesiyle değil |  |
| This draft ends on that video with its own sound, not muted | Dieser Entwurf endet mit diesem Video mit eigenem Ton, nicht stumm | Bu taslak o videoyla kendi sesiyle bitiyor, sessiz değil |  |
| This draft has a title, but you asked for none. | Dieser Entwurf hat einen Titel, aber du wolltest keinen. | Bu taslakta başlık var, ama başlık istemedin. |  |
| This draft has no pop-ins timed to your words | Dieser Entwurf hat keine Einblendungen, die zu deinen Worten getimt sind | Bu taslakta sözlerine göre çıkan görsel yok |  |
| This draft is about {round_s_facts_duration_s}s{outro}; you asked for at most {asked}s. | Dieser Entwurf ist etwa {round_s_facts_duration_s} s lang{outro}; du wolltest höchstens {asked} s. | Bu taslak yaklaşık {round_s_facts_duration_s} saniye{outro}; sen {asked_tr} istedin. |  |
| This draft is about {round_s_facts_duration_s}s{outro}; you asked for {asked}s. | Dieser Entwurf ist etwa {round_s_facts_duration_s} s lang{outro}; du wolltest {asked} s. | Bu taslak yaklaşık {round_s_facts_duration_s} saniye{outro}; sen {asked_tr} istedin. |  |
| This draft is ordered by attachment order, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, in der du die Clips hinzugefügt hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: klipleri eklediğin sıra. |  |
| This draft is ordered by attachment, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, in der du die Clips hinzugefügt hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: klipleri eklediğin sıra. |  |
| This draft is ordered by confirmed, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, die du bestätigt hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: onayladığın sıra. |  |
| This draft is ordered by creator order, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge, die du angegeben hast, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: senin verdiğin sıra. |  |
| This draft is ordered by editor, not the order you asked for. | Dieser Entwurf folgt der Reihenfolge im Editor, nicht der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: editördeki sıra. |  |
| This draft is ordered by song time, not the order you asked for. | Dieser Entwurf ist nach der Zeit im Song geordnet, nicht in der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: şarkının zamanı. |  |
| This draft is ordered by {basis_replace}, not the order you asked for. | Dieser Entwurf ist nach {basis_replace} geordnet, nicht in der gewünschten Reihenfolge. | Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: {basis_tr_get_basis_basis_replace}. |  |
| This draft keeps {shown} s of your {total} s clip, so it's cut. | Dieser Entwurf behält {shown} s von deinem {total} s langen Clip, er ist also gekürzt. | Bu taslak {total} sn'lik klibinin {shown} sn'sini tutuyor, yani klip kesiliyor. |  |
| This draft plays your song in the background; your clips aren't matched to its words. | Dieser Entwurf spielt deinen Song im Hintergrund; deine Clips sind nicht auf seinen Text abgestimmt. | Bu taslak şarkını arka planda çalıyor; klipler sözlerine göre yerleştirilmedi. |  |
| This edit belongs to a different approved revision. | Dieser Edit gehört zu einer anderen freigegebenen Version. | Bu düzenleme onaylanmış başka bir sürüme ait. |  |
| This edit can include at most {source_limit} clips. Choose fewer clips so each one can be used. | Dieser Edit kann höchstens {source_limit} Clips enthalten. Wähl weniger Clips, damit jeder verwendet werden kann. | Her biri kurguya girebilsin diye bu video için en fazla {source_limit} klip seç. |  |
| This edit can't draw a line around someone yet. | In diesem Edit kann ich noch keine Linie um jemanden zeichnen. | Bu düzenleme henüz birinin etrafına çizgi çizemiyor. |  |
| This edit can't hold your title for exactly {before_opening_title_duration_s:g} seconds. | In diesem Edit kann ich deinen Titel nicht genau {before_opening_title_duration_s:g} Sekunden lang halten. | Bu düzenleme başlığını tam {before_opening_title_duration_s:g} saniye tutamıyor. |  |
| This edit can't keep: {names}. | In diesem Edit lässt sich nicht beibehalten: {names}. | Bu düzenlemede korunamayanlar: {names}. |  |
| This edit cannot carry out that exact combination of requests. | Dieser Edit kann genau diese Kombination von Wünschen nicht umsetzen. | Bu düzenleme istediklerinin bu birleşimini aynen yapamıyor. |  |
| This edit cannot change the footage's brightness yet. | Dieser Edit kann die Helligkeit des Materials noch nicht ändern. | Bu düzenleme henüz çekimin parlaklığını değiştiremiyor. |  |
| This edit cannot render {name}, so I left it out: "{detail}". | Dieser Edit kann {name} nicht darstellen, deshalb habe ich es weggelassen: "{detail}". | Bu düzenlemede {name} gösterilemiyor, o yüzden çıkardım: "{detail}". |  |
| This edit cannot show separate text on each clip yet. | Dieser Edit kann noch nicht auf jedem Clip einen eigenen Text zeigen. | Bu düzenleme henüz her klipte ayrı yazı gösteremiyor. |  |
| This edit couldn't keep confirmed text on screen long enough. | Dieser Edit konnte bestätigten Text nicht lange genug auf dem Bildschirm halten. | Bu düzenleme onayladığın yazıyı ekranda yeterince uzun tutamadı. |  |
| This edit is missing confirmed on-screen text. | In diesem Edit fehlt bestätigter Text auf dem Bildschirm. | Bu düzenlemede onayladığın ekran yazısı eksik. |  |
| This edit keeps confirmed text on screen after the picture has ended. | Dieser Edit zeigt bestätigten Text noch, nachdem das Bild schon zu Ende ist. | Bu düzenleme onayladığın yazıyı görüntü bittikten sonra da ekranda tutuyor. |  |
| This edit needs a hero clip of at least 3 seconds plus usable supporting clips, so the hero can keep at least 60% of the video. | Dieser Edit braucht einen Hauptclip von mindestens 3 Sekunden plus brauchbare unterstützende Clips, damit der Hauptclip mindestens 60 % des Videos behält. | En az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; böylece ana klip bu videonun en az %60'ını alabilir. |  |
| This edit needs a renderer that's temporarily turned off. Try again later. | Dieser Edit braucht einen Renderer, der gerade vorübergehend ausgeschaltet ist. Versuch es später nochmal. | Bu düzenleme şu anda geçici olarak kapalı bir oluşturucuya ihtiyaç duyuyor. Daha sonra tekrar dene. |  |
| This edit needs an iPhone rendering feature that's switched off right now. Your edit and footage are saved. Try again later, or ask for a different style. | Dieser Edit braucht eine Funktion zum Erstellen auf dem iPhone, die gerade ausgeschaltet ist. Dein Edit und dein Material sind gespeichert. Versuch es später nochmal oder wünsch dir einen anderen Stil. | Bu düzenleme için gereken bir iPhone oluşturma özelliği şu anda kapalı. Düzenlemen ve çekimlerin kayıtlı. Daha sonra tekrar dene ya da farklı bir stil iste. |  |
| This edit needs your confirmed recorded voice. | Dieser Edit braucht deine bestätigte aufgenommene Stimme. | Bu düzenleme için onayladığın ses kaydın gerekiyor. |  |
| This edit plan needs approving again before it can render on your iPhone. | Dieser Edit-Plan muss nochmal freigegeben werden, bevor er auf deinem iPhone erstellt werden kann. | Bu düzenleme planının iPhone'unda videoya dönüşebilmesi için yeniden onaylanması gerekiyor. |  |
| This edit put confirmed text in the wrong place. | Dieser Edit hat bestätigten Text an die falsche Stelle gesetzt. | Bu düzenleme onayladığın yazıyı yanlış yere koydu. |  |
| This edit renders on your iPhone, and it can't use some of the media you picked. Should I make it with just this project's clips? | Dieser Edit wird auf deinem iPhone erstellt und kann einige der gewählten Medien nicht verwenden. Soll ich ihn nur mit den Clips dieses Projekts machen? | Bu düzenleme iPhone'unda hazırlanıyor ve seçtiğin bazı medyayı kullanamıyor. Sadece bu projenin kliplerini kullanarak yapayım mı? |  |
| This edit uses one of your clips, not every take. | Dieser Edit nutzt einen deiner Clips, nicht jeden Take. | Bu düzenleme kliplerinden yalnızca birini kullanıyor, her çekimi değil. |  |
| This edit uses something your iPhone can't render yet. Start a new edit with a different clip or format. | Dieser Edit nutzt etwas, das dein iPhone noch nicht erstellen kann. Starte einen neuen Edit mit einem anderen Clip oder Format. | Bu düzenleme iPhone'unun henüz oluşturamadığı bir şey içeriyor. Farklı bir klip ya da formatla yeni bir düzenleme başlat. |  |
| This edit was prepared by a newer version of Kria than the one rendering it. Ask Kria to prepare it again. | Dieser Edit wurde von einer neueren Kria-Version vorbereitet als der, die ihn erstellt. Bitte Kria, ihn nochmal vorzubereiten. | Bu düzenleme, onu oluşturan sürümden daha yeni bir Kria sürümüyle hazırlandı. Kria'dan yeniden hazırlamasını iste. |  |
| This export is no longer available. | Dieser Export ist nicht mehr verfügbar. | Bu dışa aktarma artık kullanılamıyor. |  |
| This iPhone can't mix its sound into the video yet | Dieses iPhone kann seinen Ton noch nicht ins Video mischen | Bu iPhone sesini videoya karıştıramıyor henüz |  |
| This iPhone can't play videos from Visuals yet | Dieses iPhone kann Videos aus Visuals noch nicht abspielen | Bu iPhone Visuals'taki videoları henüz oynatamıyor |  |
| This iPhone render can't place pictures and videos around your talk yet | Das Erstellen auf diesem iPhone kann Bilder und Videos noch nicht um deine Rede herum platzieren | Bu iPhone oluşturması konuşmanın çevresine resim ve video koyamıyor henüz |  |
| This kind of edit always shows captions right now, so I kept them in. | Diese Art von Edit zeigt im Moment immer Untertitel, deshalb habe ich sie drin gelassen. | Bu tür düzenleme şu an her zaman altyazı gösteriyor, o yüzden altyazıları bıraktım. |  |
| This kind of edit can't keep your text in a corner yet. Should I make it without that text? | Diese Art von Edit kann deinen Text noch nicht in einer Ecke halten. Soll ich es ohne diesen Text machen? | Bu tür bir düzenleme yazını henüz köşede tutamıyor. O yazı olmadan yapayım mı? |  |
| This kind of edit can't show this text yet. | Diese Art von Edit kann diesen Text noch nicht zeigen. | Bu tür düzenleme bu yazıyı henüz gösteremiyor. |  |
| This kind of edit can't show {missing} yet. Should I make everything else and leave that text out? | Diese Art von Edit kann {missing} noch nicht zeigen. Soll ich alles andere machen und diesen Text weglassen? | Bu tür düzenleme {missing_tr} henüz gösteremiyor. Geri kalan her şeyi yapıp o yazıyı dışarıda bırakayım mı? |  |
| This kind of video can't render on your iPhone right now. Choose a format that renders on this iPhone (Montage, or Talking to camera / Narrated where available). No fallback edit was rendered. | Diese Art von Video lässt sich gerade nicht auf deinem iPhone erstellen. Wähl ein Format, das auf diesem iPhone erstellt wird (Montage oder, wo verfügbar, In die Kamera sprechen / Narrated). Es wurde kein Ersatz-Edit erstellt. | Bu tür bir video şu anda iPhone'unda oluşturulamıyor. iPhone'da oluşturulabilen bir format seç (Montaj, ya da uygun olduğunda Kameraya konuşma / Seslendirmeli). Yedek bir düzenleme oluşturulmadı. |  |
| This montage edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Montage-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (montaj) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This narrated edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Voiceover-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (seslendirmeli) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This narrated planned edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Voiceover-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (seslendirmeli) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This narrated ready edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Voiceover-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (seslendirmeli) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This phone can't play the title animation ({words_value}) yet, so the title fades in instead | Dieses Handy kann die Titelanimation ({words_value}) noch nicht abspielen, deshalb wird der Titel stattdessen eingeblendet | Bu telefon başlık animasyonunu ({words_value}) henüz oynatamıyor, o yüzden başlık onun yerine yavaşça beliriyor |  |
| This photo isn't in the video. | Dieses Foto ist nicht im Video. | Bu fotoğraf videoda yok. |  |
| This photo plays {round_s_rendered}s; you asked for {asked:g}s. | Dieses Foto läuft {round_s_rendered} s; du wolltest {asked:g} s. | Bu fotoğraf {round_s_rendered} saniye görünüyor; sen {asked:g} saniye istedin. |  |
| This plan has a guided story but the edit format is audio-led. Pick one: the guided story, or the spoken/narrated edit. | Dieser Plan hat eine geführte Story, aber das Edit-Format richtet sich nach dem Ton. Wähl eins: die geführte Story oder den gesprochenen bzw. erzählten Edit. | Bu planda rehberli bir hikâye var ama düzenleme formatı sese dayalı. Birini seç: rehberli hikâye ya da konuşmalı/seslendirmeli düzenleme. |  |
| This project can only be rendered on our servers, and server rendering is switched off, so this version wasn't made. Start a new edit on your iPhone with these clips to make it there. | Dieses Projekt kann nur auf unseren Servern erstellt werden, und das Erstellen auf dem Server ist ausgeschaltet, deshalb wurde diese Version nicht gemacht. Starte mit diesen Clips einen neuen Edit auf deinem iPhone, um es dort zu erstellen. | Bu proje yalnızca sunucularımızda oluşturulabiliyor ve sunucuda oluşturma şu anda kapalı, bu yüzden bu sürüm yapılmadı. Bu kliplerle iPhone'unda yeni bir düzenleme başlatarak videoyu orada yap. |  |
| This project cannot render entirely on this iPhone. Its existing video is unchanged. | Dieses Projekt lässt sich nicht komplett auf diesem iPhone erstellen. Das vorhandene Video bleibt unverändert. | Bu proje tamamen bu iPhone'da oluşturulamıyor. Mevcut videon değişmedi. |  |
| This project is archived. | Dieses Projekt ist archiviert. | Bu proje arşivlendi. |  |
| This project needs footage before I can make an editorial decision. | Dieses Projekt braucht Material, bevor ich eine Schnittentscheidung treffen kann. | Düzenleme kararı verebilmem için bu projeye önce çekim eklenmesi gerekiyor. |  |
| This project's footage lives on your iPhone and can't be rendered from this account right now. | Das Material dieses Projekts liegt auf deinem iPhone und kann gerade nicht von diesem Konto aus erstellt werden. | Bu projenin çekimleri iPhone'unda duruyor ve şu anda bu hesaptan videoya dönüştürülemiyor. |  |
| This render path can't keep that requirement. | Dieser Weg zum fertigen Video kann diese Vorgabe nicht einhalten. | Bu video hazırlama yolu bu isteği koruyamıyor. |  |
| This render was cancelled. | Dieses Video wurde abgebrochen. | Bu video iptal edildi. |  |
| This renderer can't combine the confirmed soundtracks. | Dieser Renderer kann die bestätigten Tonspuren nicht kombinieren. | Bu video hazırlama yolu onayladığın sesleri birleştiremiyor. |  |
| This renderer would change the proposed edit. I need to prepare the supported version first. | Dieser Renderer würde den vorgeschlagenen Edit ändern. Ich muss zuerst die unterstützte Version vorbereiten. | Bu oluşturucu önerilen düzenlemeyi değiştirir. Önce desteklenen sürümü hazırlamam gerekiyor. |  |
| This single hero edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Edit rund um einen Hauptclip kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (tek kahraman) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This slides edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Slideshow-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (slayt) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This speech check also found restarted sentences. Update the app to review that cleanup choice, or keep the original speech. | Diese Sprachprüfung hat auch neu begonnene Sätze gefunden. Aktualisiere die App, um diese Bereinigung zu prüfen, oder behalte die Originalsprache. | Bu konuşma kontrolü yeniden başlatılan cümleler de buldu. Bu temizleme seçimini incelemek için uygulamayı güncelle ya da özgün konuşmayı koru. |  |
| This suggestion no longer fits your edit. Nothing was changed, and I'm refreshing the ideas. | Dieser Vorschlag passt nicht mehr zu deinem Edit. Es wurde nichts geändert, und ich erneuere die Ideen. | Bu öneri artık düzenlemene uymuyor. Hiçbir şey değişmedi, önerileri yeniliyorum. |  |
| This talking head edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Talking-Head-Edit kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme (konuşmalı) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| This template has a configuration problem. Try a different one. | Diese Vorlage hat ein Konfigurationsproblem. Probier eine andere. | Bu şablonda bir yapılandırma sorunu var. Farklı bir tane dene. |  |
| This template is missing assets it needs. Try a different one. | Dieser Vorlage fehlen Dateien, die sie braucht. Probier eine andere. | Bu şablonun ihtiyaç duyduğu dosyalar eksik. Farklı bir tane dene. |  |
| This text follows its narration or source timing; its timing was not changed. | Dieser Text folgt dem Timing seines Voiceovers oder seiner Quelle; sein Timing wurde nicht geändert. | Bu yazı seslendirmeyi ya da kaynağın zamanlamasını izliyor; zamanlaması değişmedi. |  |
| This version leaves out the photo you asked for: "{ask}". | In dieser Version fehlt das Foto, das du dir gewünscht hast: "{ask}". | Bu sürümde istediğin fotoğraf yok: "{ask}". |  |
| This version leaves out the video you asked for: "{ask}". | In dieser Version fehlt das Video, das du dir gewünscht hast: "{ask}". | Bu sürümde istediğin video yok: "{ask}". |  |
| This video didn't record how it framed the sideways clips, so I can't check it | Dieses Video hat nicht gespeichert, wie es die Querformat-Clips eingerahmt hat, deshalb kann ich das nicht prüfen | Bu video yatay klipleri nasıl kadrajladığını kaydetmedi, o yüzden kontrol edemiyorum |  |
| This video didn't record how it framed the speaker, so I can't check it. | Dieses Video hat nicht gespeichert, wie es die sprechende Person eingerahmt hat, deshalb kann ich das nicht prüfen. | Bu video konuşan kişiyi nasıl kadrajladığını kaydetmedi, o yüzden kontrol edemiyorum. |  |
| This video didn't record where it starts and ends, so I can't check it. | Dieses Video hat nicht gespeichert, wo es anfängt und endet, deshalb kann ich das nicht prüfen. | Bu video nerede başlayıp bittiğini kaydetmedi, o yüzden kontrol edemiyorum. |  |
| This video didn't record where it starts, so I can't check it. | Dieses Video hat nicht gespeichert, wo es anfängt, deshalb kann ich das nicht prüfen. | Bu video nereden başladığını kaydetmedi, o yüzden kontrol edemiyorum. |  |
| This video has music. | Dieses Video hat Musik. | Bu videoda müzik var. |  |
| This video has no captions. | Dieses Video hat keine Untertitel. | Bu videoda altyazı yok. |  |
| This voiceover edit renders on your iPhone, which can't show a title yet. Should I make it without the title? | Dieser Voiceover-Edit wird auf deinem iPhone erstellt, und dort kann noch kein Titel gezeigt werden. Soll ich es ohne Titel machen? | Bu seslendirmeli düzenleme iPhone'unda hazırlanıyor ve orada henüz başlık gösterilemiyor. Başlıksız yapayım mı? |  |
| This voiceover edit uses all of your project's clips. Photos and videos from Visuals can't be timed to a voiceover here yet, so I left them out. | Dieser Voiceover-Edit nutzt alle Clips deines Projekts. Fotos und Videos aus Visuals lassen sich hier noch nicht auf ein Voiceover abstimmen, deshalb habe ich sie weggelassen. | Bu seslendirmeli düzenleme projendeki tüm klipleri kullanıyor. Visuals'taki fotoğraf ve videolar henüz seslendirmeye göre zamanlanamıyor, o yüzden onları koymadım. |  |
| This voiceover edit uses all of your project's clips. Photos and videos from Visuals show up only on a word you say, so I left out the ones not tied to a word. | Dieser Voiceover-Edit nutzt alle Clips deines Projekts. Fotos und Videos aus Visuals erscheinen nur bei einem Wort, das du sagst, deshalb habe ich die weggelassen, die an kein Wort gebunden sind. | Bu seslendirmeli düzenleme projendeki tüm klipleri kullanıyor. Visuals'taki fotoğraf ve videolar sadece söylediğin bir kelimede beliriyor, o yüzden bir kelimeye bağlı olmayanları koymadım. |  |
| This {facts_edit_format_replace} edit cuts your footage down; a Talking edit keeps the whole take. | Dieser Edit ({facts_edit_format_replace}) kürzt dein Material; ein Talking-Edit behält den ganzen Take. | Bu düzenleme ({v1}) çekimlerini kısaltır; Konuşmalı düzenleme çekimin tamamını korur. |  |
| Those labels need a recorded voiceover with guided visuals. | Diese Labels brauchen ein aufgenommenes Voiceover mit geführten Visuals. | Bu etiketler için kaydedilmiş bir seslendirme ve rehberli görseller gerekiyor. |  |
| Those parts belonged to your old take. Ask me to make the video again and I'll plan it from your new one. | Diese Teile gehörten zu deinem alten Take. Bitte mich, das Video nochmal zu erstellen, dann plane ich es mit deinem neuen. | Bu bölümler eski çekimine aitti. Videoyu yeniden yapmamı iste, yeni çekiminle planlayayım. |  |
| Those photos and your footage need about {shown} seconds, more than {asked}. | Diese Fotos und dein Material brauchen etwa {shown} Sekunden, mehr als {asked}. | O fotoğraflar ve çekimlerin yaklaşık {shown} saniye istiyor, {asked} saniyeden fazla. |  |
| Those seconds are past the end of the video | Diese Sekunden liegen hinter dem Ende des Videos | O saniyeler videonun bitişinden sonra |  |
| Times are shown in UTC (your clips were filmed in more than one time zone, or one I can't tell). | Die Zeiten werden in UTC angezeigt (deine Clips wurden in mehr als einer Zeitzone gefilmt oder in einer, die ich nicht erkennen kann). | Saatler UTC olarak gösteriliyor (klipler birden fazla saat diliminde çekilmiş ya da hangisi olduğunu anlayamadım). |  |
| Times are shown in {zone_replace} time. | Die Zeiten werden in {zone_replace}-Zeit angezeigt. | Saatler şu saat diliminde gösteriliyor: {zone_replace}. |  |
| Title → {v1} | Titel → {v1} | Başlık → {v1} |  |
| To try again, ask for a new version and review its approval. | Um es nochmal zu versuchen, bitte um eine neue Version und prüfe ihre Freigabe. | Tekrar denemek için yeni bir sürüm isteyip onayını gözden geçir. |  |
| To write that text I may research facts on the web after you approve | Für diesen Text recherchiere ich nach deiner Freigabe vielleicht Fakten im Web | Bu yazıyı hazırlarken onayından sonra web'de bilgi araştırabilirim |  |
| Transcription timed out — try again | Die Transkription hat zu lange gedauert, versuch es nochmal | Yazıya dökme zaman aşımına uğradı, tekrar dene |  |
| Trim it to {asked_s} seconds | Auf {asked_s} Sekunden kürzen | {asked_s} saniyeye kısalt |  |
| Trimmed {who} | Gekürzt: {who} | {who} kırpıldı |  |
| Try a different clip of you talking, or ask for a montage. | Probier einen anderen Clip, in dem du sprichst, oder wünsch dir eine Montage. | Konuştuğun başka bir klibi dene ya da bir montaj iste. |  |
| Try again, or tell me the most important change first. | Versuch es nochmal oder sag mir zuerst die wichtigste Änderung. | Tekrar dene ya da önce en önemli değişikliği söyle. |  |
| Turkish | Türkisch |  |  |
| Turned the captions off | Untertitel ausgeschaltet | Altyazılar kapatıldı |  |
| Turned the captions on | Untertitel eingeschaltet | Altyazılar açıldı |  |
| Turning clip sound on or off by clip or by seconds works on a montage made on your iPhone, not in this edit | Den Clip-Ton pro Clip oder pro Sekunden an- oder auszuschalten geht bei einer Montage auf deinem iPhone, nicht in diesem Edit | Klip sesini klip ya da saniye bazında açıp kapatmak yalnızca iPhone'unda yapılan montajda çalışıyor, bu düzenlemede değil |  |
| Two of your clips show "{label}". Which one should I use for it? Keep that one ticked and untick the other. | Zwei deiner Clips zeigen "{label}". Welchen soll ich dafür nehmen? Lass ihn angehakt und nimm den Haken beim anderen weg. | "{label}" iki klibinde de görünüyor. Hangisini kullanayım? Onu işaretli bırak, diğerinin işaretini kaldır. |  |
| Two people on camera are too far apart for one vertical crop to hold both | Zwei Personen vor der Kamera sind zu weit auseinander für einen einzigen Hochformat-Ausschnitt | Kameradaki iki kişi tek bir dikey kadraja sığmayacak kadar birbirinden uzak |  |
| Undid: {what} | Rückgängig gemacht: {what} | Geri alındı: {what} |  |
| Undo has to be its own request. Nothing was changed. | Rückgängig machen muss eine eigene Anfrage sein. Es wurde nichts geändert. | Geri alma ayrı bir istek olmalı. Hiçbir şey değişmedi. |  |
| Updated split-screen layout and panels | Split-Screen-Layout und Felder aktualisiert | Bölünmüş ekran düzeni ve paneller güncellendi |  |
| Updated the captions | Untertitel aktualisiert | Altyazılar güncellendi |  |
| Updated the edit. | Edit aktualisiert. | Düzenlemeyi güncelledim. |  |
| Updated what I remember for future videos. | Ich habe aktualisiert, was ich mir für künftige Videos merke. | Sonraki videoların için aklımda tuttuğum şeyi güncelledim. |  |
| Updated your edit — it's in the editor now. Save when you're happy with it. | Dein Edit ist aktualisiert und jetzt im Editor. Speichere ihn, wenn er dir gefällt. | Düzenlemeni güncelledim, şu an editörde. Memnun kaldığında kaydet. |  |
| Updated your video. | Dein Video ist aktualisiert. | Videon güncellendi. |  |
| Updated {scope_phrase_left}. | {scope_phrase_left} aktualisiert. | {scope_phrase_left_capitalize} güncellendi. |  |
| Updated {v1}. | {v1} aktualisiert. | {capitalize_tr} güncelledim. |  |
| Upload the song first, then I'll build the edit around it. Or should I use licensed music instead? | Lade zuerst den Song hoch, dann baue ich den Edit darum herum. Oder soll ich stattdessen lizenzierte Musik nehmen? | Önce şarkıyı yükle, düzenlemeyi ona göre kurarım. Ya da lisanslı müzik kullanayım mı? |  |
| Use "{text}" instead | Stattdessen "{text}" nehmen | Onun yerine "{text}" yaz |  |
| Use a longer clip of you talking, or ask for a montage. | Nimm einen längeren Clip, in dem du sprichst, oder wünsch dir eine Montage. | Konuştuğun daha uzun bir klip kullan ya da bir montaj iste. |  |
| Use automatic {role} fonts and exclude {font} from selection. | Schrift für {role} automatisch wählen und {font} dabei ausschließen. | {head} için yazı tipi otomatik seçilsin, {font} seçimden çıkarılsın. |  |
| Use the available {capacity_s} seconds | Die verfügbaren {capacity_s} Sekunden nehmen | Mevcut {capacity_s} saniyeyi kullan |  |
| Use the order I give you | Reihenfolge nehmen, die ich dir gebe | Sırayı ben vereyim |  |
| Use the order you added the clips | Reihenfolge nehmen, in der du die Clips hinzugefügt hast | Klipleri eklediğin sırayı kullan |  |
| Use the supported trim for this footage. | Nutzt die unterstützte Kürzung für dieses Material. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use the supported trim for this song. | Nutzt die unterstützte Kürzung für diesen Song. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use the supported trim for this spoken take. | Nutzt die unterstützte Kürzung für diesen gesprochenen Take. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use the supported trim for this voiceover with the selected footage. | Nutzt die unterstützte Kürzung für dieses Voiceover mit dem ausgewählten Material. | Bu {source_tr} için desteklenen kırpmayı kullanır. |  |
| Use this recording's voice under the other clips. | Die Stimme dieser Aufnahme unter den anderen Clips nutzen. | Bu kaydın sesini diğer kliplerin altında kullan. |  |
| Use {color} for {role} text in future videos. | In künftigen Videos {color} für {role} verwenden. | Gelecek videolarda {role} için {color} rengi kullanılsın. |  |
| Uses {kept} of your {count} clips, spread evenly through your footage; the others are left out. | Nutzt {kept} deiner {count} Clips, gleichmäßig über dein Material verteilt; die anderen fallen weg. | {count} klibinden {kept} tanesini kullanır, çekimlerine eşit aralıklarla dağıtır; diğerleri dışarıda kalır. |  |
| Video length {first_before:g}s → {first_after:g}s | Videolänge {first_before:g} s → {first_after:g} s | Video süresi {first_before:g} sn → {first_after:g} sn |  |
| Video length {plan_intent_target_s:g}s | Videolänge {plan_intent_target_s:g} s | Video süresi {plan_intent_target_s:g} saniye |  |
| Videos can't show as small cards in this edit yet, so {name} is left out | Videos können in diesem Edit noch nicht als kleine Karten erscheinen, deshalb ist {name} weggelassen | Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor, {name} koymadım |  |
| Videos can't show as small cards in this edit yet; left that card out. | Videos können in diesem Edit noch nicht als kleine Karten erscheinen; habe die Karte weggelassen. | Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor; o kartı koymadım. |  |
| Want me to use music instead? | Soll ich stattdessen Musik nehmen? | Onun yerine müzik kullanayım mı? |  |
| We couldn't read that recording — try again | Wir konnten diese Aufnahme nicht lesen, versuch es nochmal | Bu kaydı okuyamadık, tekrar dene |  |
| We couldn't understand that recording — try again | Wir konnten diese Aufnahme nicht verstehen, versuch es nochmal | Bu kaydı anlayamadık, tekrar dene |  |
| What are we making? Pick a format and we’ll shape it together. | Was machen wir? Wähl ein Format und wir gestalten es zusammen. | Ne yapıyoruz? Bir format seç, birlikte şekillendirelim. |  |
| What shorter label should I use instead? | Welches kürzere Label soll ich stattdessen nehmen? | Yerine hangi daha kısa etiketi yazayım? |  |
| What should the caption for "{intent_attribute}" say? | Was soll der Untertitel für "{intent_attribute}" sagen? | "{intent_attribute}" için yazı ne olsun? |  |
| What should the captions say? | Was sollen die Untertitel sagen? | Altyazıda ne yazsın? |  |
| What would you like me to revise? | Was soll ich überarbeiten? | Neyi değiştirmemi istersin? |  |
| What would you like? | Was möchtest du? | Ne yapmamı istersin? |  |
| When several of your clips talk, photos on your words can only show full screen, so this one won't show | Wenn in mehreren deiner Clips gesprochen wird, können Fotos bei deinen Worten nur im Vollbild erscheinen, deshalb wird dieses nicht gezeigt | Birkaç klibinde konuştuğunda sözlerine göre çıkan fotoğraflar yalnızca tam ekran gösterilebiliyor, o yüzden bu gösterilmeyecek |  |
| When you say {v1}, clip {v2} of {len_steps} is on screen ({step_start_s:.1f}–{step_end_s:.1f} s) | Wenn du {v1} sagst, ist Clip {v2} von {len_steps} im Bild ({step_start_s:.1f}–{step_end_s:.1f} s) | {v1} dediğinde ekranda {len_steps} klipten {v2}. klip var ({step_start_s:.1f}–{step_end_s:.1f} sn) |  |
| Which clip or part of the edit should I work on first? | An welchem Clip oder Teil des Edits soll ich zuerst arbeiten? | Önce düzenlemenin hangi klibi ya da bölümü üzerinde çalışayım? |  |
| Which clips should I use for that part? | Welche Clips soll ich für diesen Teil nehmen? | Bu bölüm için hangi klipleri kullanayım? |  |
| Which complete word sequence should I retime? | Welche vollständige Wortfolge soll ich neu timen? | Hangi kelime dizisinin tamamını yeniden zamanlayayım? |  |
| Which do you prefer? | Was gefällt dir besser? | Hangisini tercih edersin? |  |
| Which frame should hold? | Welches Bild soll stehen bleiben? | Hangi kare dursun? |  |
| Which images would you like to put together, and how short should each image be? | Welche Bilder möchtest du zusammenstellen, und wie kurz soll jedes Bild sein? | Hangi görselleri bir araya getirmek istersin ve her görsel ne kadar kısa olsun? |  |
| Which line should it end on? | Mit welchem Satz soll es enden? | Hangi cümleyle bitsin? |  |
| Which line should it open on? | Mit welchem Satz soll es beginnen? | Hangi cümleyle açılsın? |  |
| Which moment should freeze? I'll hold the middle of it. | Welcher Moment soll eingefroren werden? Ich nehme die Mitte davon. | Hangi an donsun? Ortasındaki kareyi tutarım. |  |
| Which one should I cut? | Welchen soll ich schneiden? | Hangisini keseyim? |  |
| Which one should it appear on? | Auf welchem soll er erscheinen? | Yazı hangisinde çıksın? |  |
| Which supported video length should I use? | Welche unterstützte Videolänge soll ich nehmen? | Hangi desteklenen video uzunluğunu kullanayım? |  |
| Which uploaded image or video Visuals should I remove? Please identify them or say all uploaded Visuals. Nothing was changed. | Welche hochgeladenen Bild- oder Video-Visuals soll ich entfernen? Sag mir, welche, oder sag "alle hochgeladenen Visuals". Es wurde nichts geändert. | Yüklediğin görsellerden hangilerini kaldırayım? Hangileri olduğunu söyle ya da hepsini kaldırmamı iste. Hiçbir şey değişmedi. |  |
| Will do at render | Passiert beim Erstellen des Videos | Videoda yapılacak |  |
| Will locate the requested occurrence in the original speech before cutting; no guess | Ich suche die gewünschte Stelle im Originalton, bevor ich schneide; ich rate nicht | Kesmeden önce istediğin tekrarı orijinal konuşmada bulacağım; tahmin etmeyeceğim |  |
| Will reduce steady engine noise before rendering; engine and speech are not independently isolated | Ich verringere das gleichmäßige Motorgeräusch, bevor das Video erstellt wird; Motor und Sprache lassen sich nicht getrennt voneinander herauslösen | Renderdan önce sabit motor gürültüsünü azaltacağım; motor sesiyle konuşmayı birbirinden bağımsız ayıramam |  |
| With the standard ending that part runs to {asked_s} seconds or more, and I can't shorten it without changing that line. | Mit dem Standard-Ende dauert dieser Teil {asked_s} Sekunden oder länger, und ich kann ihn nicht kürzen, ohne diesen Satz zu ändern. | Standart kapanışla o kısım {asked_s} saniyeye ya da daha fazlasına çıkıyor ve {lines_tr} değiştirmeden kısaltamam. |  |
| With the standard ending that part runs to {asked_s} seconds or more, and I can't shorten it without changing those lines. | Mit dem Standard-Ende dauert dieser Teil {asked_s} Sekunden oder länger, und ich kann ihn nicht kürzen, ohne diese Sätze zu ändern. | Standart kapanışla o kısım {asked_s} saniyeye ya da daha fazlasına çıkıyor ve {lines_tr} değiştirmeden kısaltamam. |  |
| With these clips, the closest supported length is {closest:g}s. Use that length, or choose different clips? Nothing was changed. | Mit diesen Clips ist {closest:g} s die nächste unterstützte Länge. Soll ich diese Länge nehmen, oder wählst du andere Clips? Es wurde nichts geändert. | Bu kliplerle desteklenen en yakın süre {closest:g} saniye. Bu süreyi mi kullanayım, yoksa farklı klipler mi seçelim? Hiçbir şey değişmedi. |  |
| Words from your voiceover can't be shown on the clips in this edit yet, so I left those labels out. | Wörter aus deinem Voiceover können in diesem Edit noch nicht auf den Clips angezeigt werden, deshalb habe ich diese Labels weggelassen. | Seslendirmendeki sözler bu düzenlemede henüz kliplerin üzerinde gösterilemiyor, o yüzden bu etiketleri koymadım. |  |
| Write this in the captions. | Schreib das in die Untertitel. | Bunu altyazıya yaz. |  |
| Writing the on-screen text for this many clips took longer than our AI allows, so your video wasn't made. Your edit and footage are saved. Try again; if it happens again, use fewer clips or ask for less text. | Der Text auf dem Bildschirm für so viele Clips hat länger gedauert, als unsere KI erlaubt, deshalb wurde dein Video nicht erstellt. Dein Edit und dein Material sind gespeichert. Versuch es nochmal; wenn es wieder passiert, nimm weniger Clips oder wünsch dir weniger Text. | Bu kadar klip için ekrandaki yazıları hazırlamak yapay zekânın izin verdiği süreden uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Tekrar dene; yine olursa daha az klip kullan ya da daha az yazı iste. |  |
| Writing your video's captions didn't finish. Try again. | Das Schreiben des Beitragstexts für dein Video wurde nicht fertig. Versuch es nochmal. | Videonun paylaşım metnini yazma tamamlanmadı. Tekrar dene. |  |
| Yes, build it | Ja, erstellen | Evet, oluştur |  |
| You asked for "{text}" when {speaker} says "{trigger}". "{trigger}" is said {times} time in the part of your take I'm keeping, and I can't tell who is speaking. | Du wolltest "{text}", wenn {speaker} "{trigger}" sagt. "{trigger}" wird in dem Teil deines Takes, den ich behalte, {times} Mal gesagt, und ich kann nicht erkennen, wer spricht. | "{trigger}" denildiğinde "{text}" yazısını istedin. Koruduğum kısımda "{trigger}" {times} kez geçiyor ve kimin konuştuğunu ayırt edemiyorum. |  |
| You asked for "{text}" when {speaker} says "{trigger}". "{trigger}" is said {times} times in the part of your take I'm keeping, and I can't tell who is speaking. | Du wolltest "{text}", wenn {speaker} "{trigger}" sagt. "{trigger}" wird in dem Teil deines Takes, den ich behalte, {times} Mal gesagt, und ich kann nicht erkennen, wer spricht. | "{trigger}" denildiğinde "{text}" yazısını istedin. Koruduğum kısımda "{trigger}" {times} kez geçiyor ve kimin konuştuğunu ayırt edemiyorum. |  |
| You asked for a chronological video and for the clips grouped by group. | Du wolltest ein chronologisches Video und die Clips in Gruppen sortiert. | Hem kronolojik bir video hem de kliplerin {tr_word} bazında gruplanmasını istedin. |  |
| You asked for a chronological video and for the clips grouped by sport. | Du wolltest ein chronologisches Video und die Clips nach Sportart gruppiert. | Hem kronolojik bir video hem de kliplerin {tr_word} bazında gruplanmasını istedin. |  |
| You asked for a chronological video and for the clips grouped by {word}. | Du wolltest ein chronologisches Video und die Clips nach „{word}“ gruppiert. | Hem kronolojik bir video hem de kliplerin {tr_word} bazında gruplanmasını istedin. |  |
| You asked for a title on the opening, but you didn't tell me the words, and I don't write on-screen text for you. | Du wolltest einen Titel am Anfang, hast mir aber nicht gesagt, was draufstehen soll, und Text für den Bildschirm schreibe ich nicht für dich. | Açılışa bir başlık istedin ama yazılacak kelimeleri söylemedin; ekrandaki yazıları senin yerine yazmıyorum. |  |
| You asked for a video under {maximum_s:g} seconds including its ending; this video is {actual_s:g} seconds. | Du wolltest ein Video unter {maximum_s:g} Sekunden inklusive Ende; dieses Video hat {actual_s:g} Sekunden. | Kapanış dahil {maximum_s:g} saniyenin altında bir video istedin; bu video {actual_s:g} saniye. |  |
| You asked for the clips in a specific order that follows when they were filmed. | Du wolltest die Clips in einer bestimmten Reihenfolge, die sich danach richtet, wann sie gefilmt wurden. | Klipleri çekildikleri zamana göre belirli bir sırayla istedin. |  |
| You asked for the clips in the order you filmed them. | Du wolltest die Clips in der Reihenfolge, in der du sie gefilmt hast. | Klipleri çektiğin sırayla istedin. |  |
| You asked for the same clip in two places: {join_facts_sequence_conflicts}. I kept the first place you named. | Du wolltest denselben Clip an zwei Stellen: {join_facts_sequence_conflicts}. Ich habe die zuerst genannte Stelle beibehalten. | Aynı klibi iki ayrı yere istedin: {join_facts_sequence_conflicts}. İlk söylediğin yeri korudum. |  |
| You asked for {moment_count} moments from the video, but I matched {len_moments} to it. Please tell me which moments to keep or correct the clip choices before I prepare the edit. | Du wolltest {moment_count} Momente aus dem Video, ich habe aber nur {len_moments} zuordnen können. Sag mir bitte, welche Momente ich behalten soll, oder korrigiere die Clip-Auswahl, bevor ich den Edit vorbereite. | Videodan {moment_count} an istedin, ancak {len_moments} tanesini videoyla eşleştirdim. Düzenlemeyi hazırlamadan önce hangi anları tutacağımı belirtir veya klip seçimlerini düzeltir misin? |  |
| You asked me to cut "{quote}". It is said {times} times in the part of your take I'm keeping. | Du wolltest, dass ich "{quote}" herausschneide. Es wird in dem Teil deines Takes, den ich behalte, {times} Mal gesagt. | "{quote}" kısmını kesmemi istedin. Koruduğum kısımda {times} kez geçiyor. |  |
| You asked me to freeze the frame on a moment, and more than one moment in your footage fits what you described. I won't guess which one. | Du wolltest, dass ich bei einem Moment das Bild einfriere, und in deinem Material passen mehrere Momente zu deiner Beschreibung. Ich rate nicht, welcher es ist. | Bir anda kareyi dondurmamı istedin; çekimlerinde tarif ettiğine uyan birden fazla an var. Hangisi olduğunu tahmin etmeyeceğim. |  |
| You asked me to freeze the frame on that moment. I found it in {name} ({span}s), but I can't tell the exact instant, and I won't guess it. | Du wolltest, dass ich bei diesem Moment das Bild einfriere. Ich habe ihn in {name} gefunden ({span} s), kann den genauen Zeitpunkt aber nicht bestimmen und rate nicht. | O anda kareyi dondurmamı istedin. O anı {name_tr} buldum ({span}. saniyeler arası) ama tam anını bilemiyorum ve tahmin etmeyeceğim. |  |
| You asked to leave that clip out of the edit | Du wolltest diesen Clip aus dem Edit weglassen | O klibi düzenlemeden çıkarmamı istemiştin |  |
| You asked to leave that text out. Let's update the plan before rendering. | Du wolltest diesen Text weglassen. Lass uns den Plan anpassen, bevor ich das Video erstelle. | O yazının olmamasını istedin. Videoyu oluşturmadan önce planı güncelleyelim. |  |
| You can preview the draft or undo it. | Du kannst dir den Entwurf ansehen oder ihn rückgängig machen. | Taslağı önizleyebilir veya geri alabilirsin. |  |
| You can tell me which clip shows it, or reply "skip it" and I'll {then}. | Du kannst mir sagen, in welchem Clip es zu sehen ist, oder mit "überspringen" antworten, dann {then}. | Hangi klipte olduğunu söyleyebilirsin ya da "boşver" yaz, {then}. |  |
| You changed the cut in the editor, so that clip no longer plays like this | Du hast den Schnitt im Editor geändert, deshalb läuft dieser Clip nicht mehr so | Editörde kesimi değiştirdin, o yüzden o klip artık böyle çalmıyor |  |
| You changed the edit after I placed the text, so I can't confirm it still stays off {named} | Du hast den Edit geändert, nachdem ich den Text platziert hatte, deshalb kann ich nicht bestätigen, dass er immer noch nicht auf {named} liegt | Yazıyı yerleştirdikten sonra düzenlemeyi değiştirdin; hâlâ {named_tr} üstünde olmadığını doğrulayamıyorum |  |
| You chose a landscape video, so the clips fill a wide frame, not a vertical one | Du hast ein Querformat-Video gewählt, deshalb füllen die Clips ein breites Bild statt eines Hochformats | Yatay video seçtin, o yüzden klipler dikey değil geniş bir kadrajı dolduruyor |  |
| You chose a landscape video, so there was no vertical framing to do and your clip shows in the full wide frame | Du hast ein Querformat-Video gewählt, deshalb gab es keinen Hochformat-Ausschnitt anzupassen und dein Clip ist im vollen breiten Bild zu sehen | Yatay video seçtin, o yüzden yapılacak bir dikey kadraj yoktu ve klibin geniş kadrajda tam görünüyor |  |
| You chose black bars in the editor, so the whole frame shows | Du hast im Editor schwarze Balken gewählt, deshalb ist das ganze Bild zu sehen | Editörde siyah bantları seçtin, o yüzden görüntünün tamamı görünüyor |  |
| You chose black bars, so the {clips_n} keep them | Du hast schwarze Balken gewählt, deshalb bleiben sie bei {clips_n} | Siyah bantları seçtin, o yüzden {clips_n} bantlı kaldı |  |
| You chose crop in the editor, so the video is cropped to the middle and the speaker's face can leave the frame | Du hast im Editor Zuschneiden gewählt, deshalb wird das Video auf die Mitte zugeschnitten und das Gesicht der sprechenden Person kann aus dem Bild rutschen | Editörde kırpmayı seçtin, o yüzden video ortadan kırpıldı ve konuşan kişinin yüzü kadrajdan çıkabilir |  |
| You chose crop, so the {clips_n} are cropped to fill the frame | Du hast Zuschneiden gewählt, deshalb habe ich {clips_n} bildfüllend zugeschnitten | Kırpmayı seçtin, o yüzden {clips_n} ekranı dolduracak şekilde kırpıldı |  |
| You didn't pick a clip for "{label}" | Du hast keinen Clip für "{label}" ausgewählt | "{label}" için bir klip seçmedin |  |
| You didn't pick a clip for "{words}" | Du hast keinen Clip für "{words}" ausgewählt | "{words}" için bir klip seçmedin |  |
| You didn't say what order the clips in {quoted} should play in. Tell me the order, or say any order is fine. | Du hast nicht gesagt, in welcher Reihenfolge die Clips in {quoted} laufen sollen. Sag mir die Reihenfolge oder sag, dass jede Reihenfolge okay ist. | {quoted} içindeki kliplerin hangi sırayla oynayacağını söylemedin. Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle. |  |
| You didn't say what order these clips should play in. Tell me the order, or say any order is fine. | Du hast nicht gesagt, in welcher Reihenfolge diese Clips laufen sollen. Sag mir die Reihenfolge oder sag, dass jede Reihenfolge okay ist. | Bu kliplerin hangi sırayla oynayacağını söylemedin. Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle. |  |
| You gave me a specific order for the clips. | Du hast mir eine bestimmte Reihenfolge für die Clips vorgegeben. | Klipler için belirli bir sıra verdin. |  |
| You gave me the words "{clean_name_str_req_literal}" for one shot. | Du hast mir für eine Einstellung den Text "{clean_name_str_req_literal}" gegeben. | Bir sahne için "{clean_name_str_req_literal}" yazısını verdin. |  |
| You marked {named} on a different clip than the one I put the text on; mark it on the speaking clip | Du hast {named} auf einem anderen Clip markiert als dem, auf den ich den Text gelegt habe; markier es auf dem Clip, in dem gesprochen wird | {named_tr} yazının olduğu klipte değil başka bir klipte işaretledin; konuşan klipte işaretle |  |
| You moved the clips yourself, so your own order is kept. | Du hast die Clips selbst verschoben, deshalb bleibt deine Reihenfolge erhalten. | Klipleri kendin taşıdığın için senin sıran korundu. |  |
| You never talk about {ref}, so I left it out | Du sprichst nie über {ref}, deshalb habe ich es weggelassen | {ref} hakkında hiç konuşmuyorsun, o yüzden dışarıda bıraktım |  |
| You picked Lip-sync, so I'll sync your takes to your song, placing each one where it fits the music. | Du hast Lip-Sync gewählt, deshalb synchronisiere ich deine Takes mit deinem Song und setze jeden dorthin, wo er zur Musik passt. | Dudak senkronu seçtin, o yüzden çekimlerini şarkına senkronluyorum; her birini müziğe uyduğu yere koyuyorum. |  |
| You picked only photos. A montage needs at least one of your video clips too. Pick a clip, or add footage. | Du hast nur Fotos ausgewählt. Für eine Montage brauche ich auch mindestens einen deiner Videoclips. Wähl einen Clip aus oder füg Material hinzu. | Yalnızca fotoğraf seçtin. Bir montaj için videolarından en az biri de gerekiyor. Bir klip seç ya da çekim ekle. |  |
| You picked {clips} clip, so "{pin_text}" can't go on clip {pin_clip}. Which clip should it be on? | Du hast {clips} Clip ausgewählt, deshalb kann "{pin_text}" nicht auf Clip {pin_clip} kommen. Auf welchen Clip soll es stattdessen? | {clips} klip seçtin, o yüzden "{pin_text}" {pin_clip}. klibe konamaz. Hangi klibe koyayım? |  |
| You picked {clips} clips, so "{pin_text}" can't go on clip {pin_clip}. Which clip should it be on? | Du hast {clips} Clips ausgewählt, deshalb kann "{pin_text}" nicht auf Clip {pin_clip} kommen. Auf welchen Clip soll es stattdessen? | {clips} klip seçtin, o yüzden "{pin_text}" {pin_clip}. klibe konamaz. Hangi klibe koyayım? |  |
| You say {line} only before your start line, so the video runs to the end of your take | Du sagst {line} nur vor deinem Startsatz, deshalb läuft das Video bis zum Ende deines Takes | {line} cümlesini yalnızca başlangıç cümlenden önce söylüyorsun, bu yüzden video çekimin sonuna kadar sürüyor |  |
| You say {word} right at the end, so there was no time to show {text} | Du sagst {word} ganz am Ende, deshalb war keine Zeit mehr, {text} zu zeigen | {word} en sonda söylüyorsun, o yüzden {text} yazısına yer kalmadı |  |
| You stay full size, not small in a corner. | Du bleibst in voller Größe, nicht klein in einer Ecke. | Tam boyutta kalıyorsun, köşede küçük değilsin. |  |
| You stay full size, not small in a corner; your iPhone can't do that yet | Du bleibst in voller Größe, nicht klein in einer Ecke; dein iPhone kann das noch nicht | Tam boyutta kalıyorsun, köşede küçük değilsin; iPhone'un bunu henüz yapamıyor |  |
| You swapped in a new take, so I cancelled the plan I had prepared for the old one. Nothing was rendered. Tell me what you want and I'll plan it again with your new take. | Du hast einen neuen Take eingesetzt, deshalb habe ich den Plan für den alten verworfen. Es wurde kein Video erstellt. Sag mir, was du möchtest, und ich plane es mit deinem neuen Take neu. | Yeni çekimini ekledin, o yüzden eski çekim için hazırladığım planı iptal ettim. Hiçbir video oluşturulmadı. Ne istediğini söyle, yeni çekiminle yeniden planlayayım. |  |
| You tell me the order of the clips. | Du sagst mir die Reihenfolge der Clips. | Klip sırasını sen söylersin. |  |
| You'll start full screen in your own room, then you're {corner_0} when you start on your first recording. | Du startest im Vollbild in deinem eigenen Raum, dann bist du {corner_0}, sobald deine erste Aufnahme beginnt. | {v2}, sonra ilk kaydına geçince {corner_1}sın. |  |
| You'll start full screen in your own room, then your background takes the screen. | Du startest im Vollbild in deinem eigenen Raum, dann übernimmt dein Hintergrund den Bildschirm. | {v2}, sonra arka plan ekranı alıyor. |  |
| You'll start full screen in your own room, then {corner_0}. | Du startest im Vollbild in deinem eigenen Raum, danach bist du {corner_0}. | {v2}, sonra {corner_1}sın. |  |
| You're full screen for the whole video: there's no photo or video behind you to move into. | Du bist das ganze Video über im Vollbild: Es gibt kein Foto oder Video hinter dir, in das du wechseln könntest. | Videonun tamamında tam ekransın: geçeceğin bir arka plan fotoğrafı ya da videosu yok. |  |
| You're full screen in your own take for the first {seconds} s, then your background takes the screen. | Du bist in den ersten {seconds} s deines eigenen Takes im Vollbild, dann übernimmt dein Hintergrund den Bildschirm. | {v2}, sonra arka plan ekranı alıyor. |  |
| You're full screen in your own take for the first {seconds} s, then {corner_0}. | Du bist in den ersten {seconds} s deines eigenen Takes im Vollbild, danach bist du {corner_0}. | {v2}, sonra {corner_1}sın. |  |
| You're {corner_0} over your background, so you don't fill the frame. | Du bist {corner_0} vor deinem Hintergrund zu sehen, deshalb füllst du das Bild nicht aus. | Arka planın üstünde {corner_1}sın, o yüzden kadrajı doldurmuyorsun. |  |
| Your Visuals change was saved, but rendering could not start. Retry the render to update the exported video. | Deine Visuals-Änderung wurde gespeichert, aber das Video konnte nicht erstellt werden. Versuch es nochmal, um das exportierte Video zu aktualisieren. | Görsel değişikliğin kaydedildi ama video hazırlanmaya başlayamadı. Dışa aktarılan videoyu güncellemek için yeniden dene. |  |
| Your Visuals play full screen, but spread evenly across the talk, not timed to the word you said | Deine Visuals laufen im Vollbild, sind aber gleichmäßig über die Rede verteilt und nicht auf das Wort abgestimmt, das du gesagt hast | Görsellerin tam ekran çıkıyor ama konuşmaya eşit aralıklarla dağılıyor, söylediğin kelimeye göre zamanlanmıyor |  |
| Your approved render is in progress. Your draft is saved. | Dein freigegebenes Video wird gerade erstellt. Dein Entwurf ist gespeichert. | Onayladığın video hazırlanıyor. Taslağın kaydedildi. |  |
| Your brief says "{said}" and the photos you timed take {clean_seconds_held} seconds. | In deiner Anfrage steht "{said}", und die Fotos, die du getimt hast, dauern {clean_seconds_held} Sekunden. | İsteğinde "{said}" yazıyor ve süre verdiğin fotoğraflar {clean_seconds_held} saniye tutuyor. |  |
| Your brief says "{said}" and this edit uses {count} clips. | In deiner Anfrage steht "{said}", und dieser Edit nutzt {count} Clips. | İsteğinde "{said}" yazıyor ama bu düzenlemede {count} klip var. |  |
| Your brief says "{said}" and your voice runs {voice_s} seconds. | In deiner Anfrage steht "{said}", und deine Stimme läuft {voice_s} Sekunden. | İsteğinde "{said}" yazıyor ve sesin {voice_s} saniye sürüyor. |  |
| Your brief says "{said}", but the available footage is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber das verfügbare Material reicht für {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your brief says "{said}", but the available song is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber der verfügbare Song dauert {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your brief says "{said}", but the available spoken take is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber der verfügbare gesprochene Take dauert {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your brief says "{said}", but the available voiceover with the selected footage is {capacity_s} seconds. | In deiner Anfrage steht "{said}", aber das verfügbare Voiceover mit dem ausgewählten Material dauert {capacity_s} Sekunden. | İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye. |  |
| Your change was saved, but rendering could not start. Retry the render to update the exported video. | Deine Änderung wurde gespeichert, aber das Video konnte nicht erstellt werden. Versuch es nochmal, um das exportierte Video zu aktualisieren. | Değişikliğin kaydedildi ama video hazırlanmaya başlayamadı. Dışa aktarılan videoyu güncellemek için yeniden dene. |  |
| Your chosen clip differs from the source type you requested. Please confirm the source type or change the clip choice. | Der gewählte Clip passt nicht zur gewünschten Quellenart. Bestätige bitte die Quellenart oder ändere die Clip-Auswahl. | Seçtiğin klip istediğin kaynak türünden farklı. Lütfen kaynak türünü onayla veya klip seçimini değiştir. |  |
| Your clip is already vertical, so it fills the frame | Dein Clip ist schon im Hochformat, deshalb füllt er das Bild | Klibin zaten dikey, o yüzden ekranı dolduruyor |  |
| Your clips add up to about {possible:g} seconds, so the video can't be {wanted:g} seconds. | Deine Clips ergeben zusammen etwa {possible:g} Sekunden, deshalb kann das Video nicht {wanted:g} Sekunden lang sein. | Klipler toplam yaklaşık {possible:g} saniye, bu yüzden video {wanted:g} saniye olamıyor. |  |
| Your clips and photos have no filming time, so I can't put them in the order you filmed. | Keiner deiner Clips und Fotos hat eine Aufnahmezeit, deshalb kann ich sie nicht in die Reihenfolge deiner Aufnahmen bringen. | Kliplerinin ve fotoğraflarının hiçbiri için çekim saati yok, o yüzden onları çektiğin sıraya koyamıyorum. |  |
| Your clips are in filming order, but {join_said}. | Deine Clips liegen in Aufnahmereihenfolge, aber {join_said}. | Klipler çekim sırasında, ama {join_said}. |  |
| Your clips changed while I was planning. Your draft is unchanged; please ask again using the current clips. | Deine Clips haben sich geändert, während ich geplant habe. Dein Entwurf ist unverändert; frag bitte nochmal mit den aktuellen Clips. | Ben planlarken kliplerin değişti. Taslağın değişmedi; lütfen güncel kliplerle tekrar iste. |  |
| Your clips follow your voice, so once the video is made I'll check what's on screen when you say those words | Deine Clips folgen deiner Stimme. Sobald das Video fertig ist, prüfe ich, was auf dem Bildschirm zu sehen ist, wenn du diese Worte sagst | Klipler sesini takip ediyor, video hazır olunca o sözleri söylediğinde ekranda ne olduğunu kontrol edeceğim |  |
| Your clips keep their own sound, so your narration plays in the video. | Deine Clips behalten ihren eigenen Ton, deshalb ist deine Erzählung im Video zu hören. | Kliplerin kendi sesini koruyorum, yani anlatımın videoda duyulacak. |  |
| Your clips were filmed starting at {saw_first} and ending at {saw_last}, the reverse of the route you gave ({short_start} → {short_end}). I kept filming order; tell me if you want your route order instead. | Deine Clips wurden von {saw_first} bis {saw_last} gefilmt, also genau andersherum als die Route, die du genannt hast ({short_start} → {short_end}). Ich habe die Aufnahmereihenfolge beibehalten; sag mir, wenn du stattdessen deine Routenreihenfolge willst. | Klipler şöyle çekilmiş: başlangıç {saw_first}, bitiş {saw_last}. Bu, verdiğin rotanın ({short_start} → {short_end}) tersi. Çekim sırasını korudum; rota sırasını istersen söyle. |  |
| Your complete brief exceeds the clip planner's 12,000-character limit. | Deine komplette Anfrage überschreitet das Limit des Clip-Planers von 12.000 Zeichen. | Tüm isteğin, klip planlayıcının 12.000 karakterlik sınırını aşıyor. |  |
| Your complete request exceeds the context this planning step can safely read. | Deine komplette Anfrage ist länger, als dieser Planungsschritt sicher lesen kann. | Tüm isteğin, bu planlama adımının güvenle okuyabileceği sınırı aşıyor. |  |
| Your confirmed edit lengths conflict, so I can't render it safely. | Deine bestätigten Edit-Längen widersprechen sich, deshalb kann ich das Video nicht sicher erstellen. | Onayladığın uzunluklar birbiriyle çelişiyor, o yüzden videoyu güvenle oluşturamıyorum. |  |
| Your current draft is unchanged. | Dein aktueller Entwurf ist unverändert. | Mevcut taslağın değişmedi. |  |
| Your current draft is unchanged. Should I make a simpler version? | Dein aktueller Entwurf ist unverändert. Soll ich eine einfachere Version machen? | Mevcut taslağın değişmedi. Daha sade bir sürüm yapayım mı? |  |
| Your draft is ready. Review the pinned approval before I start the render. | Dein Entwurf ist fertig. Sieh dir die angeheftete Freigabe an, bevor ich das Video erstelle. | Taslağın hazır. Videoyu oluşturmaya başlamadan önce sabitlenen onayı incele. |  |
| Your draft is saved. | Dein Entwurf ist gespeichert. | Taslağın kaydedildi. |  |
| Your draft is saved. Should I try again or simplify this request? | Dein Entwurf ist gespeichert. Soll ich es nochmal versuchen oder die Anfrage vereinfachen? | Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa isteği sadeleştireyim mi? |  |
| Your edit instructions are too long for me to match safely. Please start a new request with the key clip directions. | Deine Edit-Anweisungen sind zu lang, als dass ich sie sicher zuordnen könnte. Starte bitte eine neue Anfrage mit den wichtigsten Clip-Angaben. | Düzenleme talimatların, kliplerle güvenle eşleştirmem için çok uzun. Lütfen önemli klip yönlendirmelerini içeren yeni bir istek başlat. |  |
| Your edit is still rendering — give it a moment, then ask again. | Dein Edit wird noch erstellt. Gib ihm einen Moment und frag dann nochmal. | Düzenlemen hâlâ hazırlanıyor, biraz bekle ve sonra tekrar iste. |  |
| Your end line keeps about {kept_s} seconds of your take: from the start to right after {end}. | Dein Schlusssatz behält etwa {kept_s} Sekunden deines Takes: vom Anfang bis kurz nach {end}. | Bitiş cümlen çekiminin yaklaşık {kept_s} saniyesini tutuyor: baştan {end} cümlesinin hemen sonrasına kadar. |  |
| Your end-screen words are too long to fit on one screen; shorten them | Dein Text für das Endbild ist zu lang für ein Bild; kürz ihn bitte | Son ekran yazın tek bir ekrana sığmayacak kadar uzun; kısalt |  |
| Your end-screen words have a character I can't draw on the card | In deinem Text für das Endbild ist ein Zeichen, das ich nicht darstellen kann | Son ekran yazında kartta çizemediğim bir karakter var |  |
| Your finished video couldn't be saved. Try again. | Dein fertiges Video konnte nicht gespeichert werden. Versuch es nochmal. | Hazır videon kaydedilemedi. Tekrar dene. |  |
| Your footage sound is muted under your voice | Der Ton deines Materials ist unter deiner Stimme stumm | Çekim sesi sesinin altında kapalı |  |
| Your footage sound plays under your voice | Der Ton deines Materials läuft unter deiner Stimme | Çekim sesi sesinin altında çalıyor |  |
| Your footage sound sits under your voice at about {pct}% volume | Der Ton deines Materials liegt mit etwa {pct}% Lautstärke unter deiner Stimme | Çekim sesi, sesinin altında yaklaşık %{pct} seviyesinde |  |
| Your full edit request is too long to preserve safely. Nothing was changed; please start a new request with the key directions. | Deine komplette Edit-Anfrage ist zu lang, um sie sicher zu übernehmen. Es wurde nichts geändert; starte bitte eine neue Anfrage mit den wichtigsten Angaben. | Tam düzenleme isteğin güvenle korumak için çok uzun. Hiçbir şey değişmedi; önemli yönergelerle yeni bir istek başlat. |  |
| Your iPhone app is too old to end on a full-screen colour with your text; update it first | Deine iPhone-App ist zu alt für ein Ende mit Farbbild und Text; aktualisier sie zuerst | iPhone uygulaman videoyu yazılı tam ekran bir renkle bitirmek için çok eski; önce güncelle |  |
| Your iPhone app is too old to put a photo or video behind you; update it first | Deine iPhone-App ist zu alt, um ein Foto oder Video hinter dich zu legen; aktualisiere sie zuerst | iPhone uygulaman arkana fotoğraf ya da video koymak için çok eski; önce güncelle |  |
| Your iPhone app is too old to put a plain colour behind you; update it first | Deine iPhone-App ist zu alt, um eine einfarbige Fläche hinter dich zu legen; aktualisiere sie zuerst | iPhone uygulaman arkana düz renk koymak için çok eski; önce güncelle |  |
| Your iPhone couldn't build that clip's own sound, so the video plays without it | Dein iPhone konnte den eigenen Ton dieses Clips nicht aufbauen, deshalb läuft das Video ohne ihn | iPhone'un o klibin kendi sesini oluşturamadı, o yüzden video o ses olmadan çalıyor |  |
| Your iPhone couldn't build that, so those clips keep their sound | Dein iPhone konnte das nicht aufbauen, deshalb behalten diese Clips ihren Ton | iPhone'un bunu hazırlayamadı, o yüzden o klipler seslerini koruyor |  |
| Your iPhone couldn't finish the render. Your approved edit is still saved. Retry the same edit on your iPhone after reviewing its approval. | Dein iPhone konnte das Video nicht fertig erstellen. Dein freigegebener Edit ist weiterhin gespeichert. Sieh dir die Freigabe an und versuch denselben Edit dann nochmal auf deinem iPhone. | iPhone'un videoyu tamamlayamadı. Onayladığın düzenleme hâlâ kayıtlı: iPhone'unda aynı düzenlemeyi yeniden denemeden önce onayını gözden geçir. |  |
| Your iPhone couldn't finish the video | Dein iPhone konnte das Video nicht fertigstellen | iPhone'un videoyu tamamlayamadı |  |
| Your iPhone stopped this export. Your approved edit is saved. Check the device error before trying again. | Dein iPhone hat diesen Export abgebrochen. Dein freigegebener Edit ist gespeichert. Sieh dir den Gerätefehler an, bevor du es nochmal versuchst. | iPhone'un bu dışa aktarımı durdurdu. Onayladığın düzenleme kayıtlı. Yeniden denemeden önce cihazdaki hatayı kontrol et. |  |
| Your image is ready. Review it before it goes in your video. Nothing in the edit changed. | Dein Bild ist fertig. Sieh es dir an, bevor es ins Video kommt. Am Edit hat sich nichts geändert. | Görselin hazır. Videoya girmeden önce gözden geçir. Düzenlemede hiçbir şey değişmedi. |  |
| Your last clip introduces someone ({quote}) with nothing before it, so I end on that clip without those words. Tell me if you want the line heard. | Dein letzter Clip stellt jemanden vor ({quote}), ohne dass davor etwas kommt, deshalb ende ich auf diesem Clip ohne diese Worte. Sag mir, wenn der Satz zu hören sein soll. | Son klibin birini tanıtıyor ({quote}) ama öncesinde bir şey yok; bu yüzden videoyu o klipte bu sözler olmadan bitiriyorum. Cümlenin duyulmasını istersen söyle. |  |
| Your last clip introduces someone ({quote}), so the video ends on that introduction. Tell me if it should come earlier. | Dein letzter Clip stellt jemanden vor ({quote}), deshalb endet das Video mit dieser Vorstellung. Sag mir, wenn sie früher kommen soll. | Son klibin birini tanıtıyor ({quote}); video bu tanıtımla bitiyor. Daha önce gelmesini istersen söyle. |  |
| Your last good version is still available. | Deine letzte gute Version ist weiterhin da. | Son iyi sürümün hâlâ duruyor. |  |
| Your length choice is still waiting. | Deine Längenauswahl steht noch aus. | Uzunluk seçimin hâlâ bekliyor. |  |
| Your length choice is still waiting. Choose an offered option or update the footage. | Deine Längenauswahl steht noch aus. Wähl eine der angebotenen Optionen oder aktualisiere das Material. | Uzunluk seçimin hâlâ bekliyor. Önerilen seçeneklerden birini seç ya da çekimlerini güncelle. |  |
| Your line is on screen for {shown} s of the {length} s video, not the whole video. | Dein Text ist {shown} s des {length}-s-Videos zu sehen, nicht im ganzen Video. | Yazın {length} sn'lik videonun {shown} sn'sinde görünüyor, videonun tamamında değil. |  |
| Your line shows only for the first {shown} s of the {length} s video, not the whole video. | Dein Text ist nur in den ersten {shown} s des {length}-s-Videos zu sehen, nicht im ganzen Video. | Yazın {length} sn'lik videonun yalnızca ilk {shown} sn'sinde görünüyor, videonun tamamında değil. |  |
| Your moments can't all get their own part of this clip at this length. Ask for a different length, or name fewer moments. | Nicht alle deine Momente bekommen bei dieser Länge einen eigenen Abschnitt in diesem Clip. Wünsch dir eine andere Länge oder nenne weniger Momente. | Saydığın anların hepsi bu uzunlukta klipte kendi yerini alamıyor. Başka bir uzunluk iste ya da daha az an say. |  |
| Your own song can only be used in videos made on your iPhone right now. Ask for this edit without your song. No fallback edit was rendered. | Dein eigener Song kann gerade nur in Videos genutzt werden, die auf deinem iPhone erstellt werden. Wünsch dir diesen Edit ohne deinen Song. Es wurde kein Ersatz-Edit erstellt. | Kendi şarkın şu anda yalnızca iPhone'unda hazırlanan videolarda kullanılabiliyor. Bu düzenlemeyi şarkın olmadan iste. Yedek bir düzenleme oluşturulmadı. |  |
| Your own song can't be changed from chat on this edit. | Deinen eigenen Song kann ich in diesem Edit nicht per Chat ändern. | Bu düzenlemede kendi şarkını sohbetten değiştiremiyorum. |  |
| Your own song only works on edits that render on your iPhone. Should I make this with licensed music instead? | Dein eigener Song funktioniert nur bei Edits, die auf deinem iPhone erstellt werden. Soll ich das stattdessen mit lizenzierter Musik machen? | Kendi şarkın sadece iPhone'unda hazırlanan düzenlemelerde çalışıyor. Bunu lisanslı müzikle yapayım mı? |  |
| Your recordings are zoomed in on their text. | Deine Aufnahmen sind auf ihren Text herangezoomt. | Kayıtlar yazılarına yakınlaştırıldı. |  |
| Your render is queued. I’ll keep watching it and update this chat when it starts. | Dein Video steht in der Warteschlange. Ich behalte es im Blick und schreibe hier im Chat, sobald es startet. | Videon sırada. Takipteyim, başlayınca bu sohbete yazacağım. |  |
| Your saved request is too large for this update. Your video is unchanged. | Deine gespeicherte Anfrage ist zu groß für dieses Update. Dein Video ist unverändert. | Kayıtlı isteğin bu güncelleme için çok büyük. Videon değişmedi. |  |
| Your saved request is too large or unavailable for this editor step. Your draft is unchanged. Which clip or part should I work on first? | Deine gespeicherte Anfrage ist für diesen Editor-Schritt zu groß oder nicht verfügbar. Dein Entwurf ist unverändert. An welchem Clip oder Teil soll ich zuerst arbeiten? | Kayıtlı isteğin bu düzenleme adımı için çok büyük ya da şu an ulaşılamıyor. Taslağın değişmedi. Önce hangi klip ya da bölüm üzerinde çalışayım? |  |
| Your song | Dein Song | Şarkın |  |
| Your song is already taken off this edit. | Dein Song ist in diesem Edit schon entfernt. | Şarkın bu düzenlemeden zaten kaldırıldı. |  |
| Your source clips changed after this draft was approved. Your draft is saved; please make a fresh plan before rendering. | Deine Quellclips haben sich geändert, nachdem dieser Entwurf freigegeben wurde. Dein Entwurf ist gespeichert; lass bitte einen neuen Plan erstellen, bevor das Video erstellt wird. | Bu taslak onaylandıktan sonra kaynak kliplerin değişti. Taslağın kayıtlı; videoyu oluşturmadan önce lütfen yeni bir plan hazırlat. |  |
| Your start and end lines keep about {kept_s} seconds of your take: from {start} to right after {end}. | Dein Startsatz und dein Schlusssatz behalten etwa {kept_s} Sekunden deines Takes: von {start} bis kurz nach {end}. | Başlangıç ve bitiş cümlelerin çekiminin yaklaşık {kept_s} saniyesini tutuyor: {start} cümlesinden {end} cümlesinin hemen sonrasına kadar. |  |
| Your start line keeps about {kept_s} seconds of your take: from {start} to the end. | Dein Startsatz behält etwa {kept_s} Sekunden deines Takes: von {start} bis zum Ende. | Başlangıç cümlen çekiminin yaklaşık {kept_s} saniyesini tutuyor: {start} cümlesinden sona kadar. |  |
| Your text colour wouldn't be readable on that background, so the text is black. | Deine Textfarbe wäre auf diesem Hintergrund nicht lesbar, deshalb ist der Text schwarz. | Yazı rengin o arka planda okunmazdı, o yüzden yazı siyah. |  |
| Your text colour wouldn't be readable on that background, so the text is white. | Deine Textfarbe wäre auf diesem Hintergrund nicht lesbar, deshalb ist der Text weiß. | Yazı rengin o arka planda okunmazdı, o yüzden yazı beyaz. |  |
| Your text covers your face: I found no spot clear of it | Dein Text verdeckt dein Gesicht: Ich habe keine Stelle gefunden, an der es frei bleibt | Yazın yüzünün üstünde kalıyor: yüzünü açıkta bırakan bir yer bulamadım |  |
| Your text ends at {exc_required_duration_s:g}s, beyond the requested {exc_requested_duration_s:g}s. Keep the video at least {exc_minimum_duration_s:g}s, or allow me to adjust the text timing? Nothing was changed. | Dein Text endet bei {exc_required_duration_s:g} s, also nach den gewünschten {exc_requested_duration_s:g} s. Soll das Video mindestens {exc_minimum_duration_s:g} s lang bleiben, oder darf ich das Timing des Textes anpassen? Es wurde nichts geändert. | Yazıların {exc_required_duration_s:g}. saniyede bitiyor. Videoyu en az {exc_minimum_duration_s:g} saniye mi tutayım, yoksa yazıların zamanlamasını değiştirebilir miyim? Hiçbir şey değişmedi. |  |
| Your video changed — reopen the editor and try again. | Dein Video hat sich geändert. Öffne den Editor neu und versuch es nochmal. | Videon değişti. Editörü yeniden açıp tekrar dene. |  |
| Your video has no sound, so there's nothing to caption or cut. | Dein Video hat keinen Ton, deshalb gibt es nichts zu untertiteln oder zu schneiden. | Videonda hiç ses yok, o yüzden altyazı eklenecek ya da kesilecek bir şey yok. |  |
| Your video has no sound, so there's nothing to caption or cut. Record it again and check that nothing is covering the microphone. | Dein Video hat keinen Ton, deshalb gibt es nichts zu untertiteln oder zu schneiden. Nimm es nochmal auf und achte darauf, dass nichts das Mikrofon verdeckt. | Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey bulamadım. Tekrar çek ve mikrofonun kapalı olmadığından emin ol. |  |
| Your video wasn't made | Dein Video wurde nicht erstellt | Videon oluşturulmadı |  |
| Your video wasn't made: the edit didn't keep something you asked for. | Dein Video wurde nicht erstellt: Der Edit hat etwas nicht übernommen, worum du gebeten hast. | Videon oluşturulmadı: düzenleme istediğin bir şeyi korumadı. |  |
| Your video wasn't made: the text {quoted} isn't in the edit. Want me to add it, or leave it out? | Dein Video wurde nicht erstellt: Der Text {quoted} ist nicht im Edit. Soll ich ihn hinzufügen oder weglassen? | Videon oluşturulmadı: {quoted} yazısı düzenlemede yok. Ekleyeyim mi, yoksa bu yazı olmadan mı yapayım? |  |
| Your voice plays first, then the footage carries on in silence. | Zuerst läuft deine Stimme, dann geht das Material still weiter. | Önce sesin çalar, sonra çekim sessizce devam eder. |  |
| Your voice runs {clean_seconds_length} seconds. | Deine Stimme läuft {clean_seconds_length} Sekunden. | Sesin {clean_seconds_length} saniye sürüyor. |  |
| Your voiceover | Dein Voiceover | Seslendirmen |  |
| Your voiceover mentions {joined} in a different order, so I followed the voiceover. | In deinem Voiceover kommt {joined} in einer anderen Reihenfolge vor, deshalb bin ich dem Voiceover gefolgt. | Seslendirmende {joined_2} farklı bir sırayla geçiyor, ben de seslendirmeyi takip ettim. |  |
| Your words never play over the other footage | Deine Worte laufen nie über dem anderen Material | Sözlerin diğer çekimlerin üzerinde hiç çalmıyor |  |
| Zoomed {who} to {zoom} | {who} auf {zoom} herangezoomt | {who} {zoom} yakınlaştırıldı |  |
| a Visual | ein Visual | bir görsel |  |
| a fast montage keeps its short cuts | Eine schnelle Montage behält ihre kurzen Schnitte | hızlı montaj kısa kesimlerini koruyor |  |
| a label on each shot | ein Label auf jeder Einstellung | her çekimdeki etiket |  |
| a moment | ein Moment | bir an |  |
| a slide post shows each slide for its own time | Ein Slide-Post zeigt jede Slide so lange, wie sie selbst dauert | slayt gönderisi her slaytı kendi süresince gösteriyor |  |
| a slide post shows your slides in order | Ein Slide-Post zeigt deine Slides der Reihe nach | slayt gönderisi slaytlarını sırayla gösteriyor |  |
| a slide post's length follows its slides | Die Länge eines Slide-Posts richtet sich nach seinen Slides | slayt gönderisinin uzunluğu slaytlarına bağlı |  |
| a video can't be a card on your iPhone yet | ein Video kann auf deinem iPhone noch keine Karte sein | video henüz iPhone'da kart olamıyor |  |
| add that effect | diesen Effekt hinzufügen | o efekti eklemek |  |
| alignment | Ausrichtung | hizalama |  |
| all {count} texts | alle {count} Texte | {count} yazının hepsi |  |
| all_text | alle Texte | tüm yazılar |  |
| an instruction | eine Anweisung | bir talimat |  |
| analyze clips | Clips analysieren | analyze clips |  |
| animated text isn't available on this phone build yet, so the text will appear without animation | animierter Text ist in dieser App-Version auf dem Handy noch nicht verfügbar, deshalb erscheint der Text ohne Animation | animasyonlu yazı bu telefon sürümünde henüz yok, bu yüzden yazı animasyonsuz görünecek |  |
| assemble | Zusammensetzen | assemble |  |
| at most {asked}s | höchstens {asked} s | en fazla {asked} saniye |  |
| at the second you picked ({at}s into the clip) | an der Sekunde, die du gewählt hast ({at} s im Clip) | seçtiğin saniyede (klibin {at}. saniyesi) |  |
| balanced | ausgewogen | balanced |  |
| both texts | beide Texte | iki yazı |  |
| bottom | unten | alt |  |
| bottom left | unten links | sol alt |  |
| bottom right | unten rechts | sağ alt |  |
| caption | Untertitel | altyazı |  |
| captions | Untertitel | altyazılar |  |
| change the backgrounds behind you | die Hintergründe hinter dir ändern | arkandaki arka planları değiştirmek |  |
| change the edit direction | die Richtung des Edits ändern | kurgunun yönünü değiştirmek |  |
| change {target} | {target} ändern | {target} için değişiklik yapmak |  |
| clip | Clip | klip |  |
| clip {number} | Clip {number} | {number}. klip |  |
| clip {ordered_0} | Clip {ordered_0} | klip {ordered_0} |  |
| clip {tail} | Clip {tail} | {tail}. klip |  |
| clip {videos} | Clip {videos} | klip {videos} |  |
| clips | Clips | klipler |  |
| clips stay in the order you filmed them, so· | die Clips bleiben in der Reihenfolge, in der du sie gefilmt hast, deshalb gilt:· | klipler çektiğin sırada kalıyor, o yüzden· |  |
| clips {head} and {tail} | Clips {head} und {tail} | {head_tr} ve {tail}. klipler |  |
| clips {labels} and {more} more | Clips {labels} und {more} weitere | klip {labels} ve {more} klip daha |  |
| clips {v1} and {v2} | Clips {v1} und {v2} | klip {v1} ve {v2} |  |
| color | Farbe | renk |  |
| couldn't be placed | konnte nicht platziert werden | yerleştirilemedi |  |
| crossfades between clips | weiche Übergänge zwischen den Clips | klipler arası yumuşak geçiş |  |
| curved | gebogen | kavisli |  |
| cutting it out of your voiceover failed | das Herausschneiden aus deinem Voiceover ist fehlgeschlagen | seslendirmenden kesmek başarısız oldu |  |
| cutting it would leave too little video | wenn ich es schneide, bleibt zu wenig Video übrig | kesersem çok az video kalırdı |  |
| day vlog | Tagesvlog | day vlog |  |
| download clips | Clips laden | download clips |  |
| end with {last} | mit {last} enden | {last} ile bitir |  |
| entrance animation | Eingangsanimation | giriş animasyonu |  |
| every clip already has its label | jeder Clip hat schon sein Label | Her klibin etiketi zaten var. |  |
| exclude | Weglassen |  |  |
| fast | schnell | fast |  |
| fast montage | schnelle Montage | fast montage |  |
| finalize | Fertigstellen | finalize |  |
| first | zuerst | ilk |  |
| first clip you added | ersten Clip, den du hinzugefügt hast | eklediğin ilk klip |  |
| first clip you filmed | ersten Clip, den du gefilmt hast | çektiğin ilk klip |  |
| font | Schriftart | yazı tipi |  |
| full-screen Visuals | Vollbild-Visuals | tam ekran Visuals |  |
| generate copy | Texte schreiben | generate copy |  |
| group | Gruppe |  |  |
| group by {what} | nach {what} gruppieren | gruplama: {what} |  |
| guided story | geführte Story | guided story |  |
| hide_part | Teil ausblenden |  |  |
| how long your title stays | wie lange dein Titel stehen bleibt | başlığının ekranda kalma süresi |  |
| in the order the clips were added | in der Reihenfolge, in der die Clips hinzugefügt wurden | klipleri eklediğin sırayla |  |
| in the order you filmed them | in der Reihenfolge, in der du sie gefilmt hast | çektiğin sırayla |  |
| include | Einbeziehen |  |  |
| it has a hidden control character a label can't show | es enthält ein verstecktes Steuerzeichen, das ein Label nicht anzeigen kann | etiketin gösteremeyeceği gizli bir kontrol karakteri var |  |
| it has nothing to show | es gibt nichts anzuzeigen | gösterilecek bir şey yok |  |
| it is cut around the spoken lines | Es wird rund um die gesprochenen Sätze geschnitten | konuşulan cümlelerin etrafında kesiliyor |  |
| it is cut as a fast montage | Es wird als schnelle Montage geschnitten | hızlı bir montaj olarak kesiliyor |  |
| it is cut as a montage under your voiceover | Es wird als Montage unter deinem Voiceover geschnitten | seslendirmenin altında bir montaj olarak kesiliyor |  |
| it is cut to your song | Es wird passend zu deinem Song geschnitten | şarkına göre kesiliyor |  |
| it isn't on the finished video | es ist nicht im fertigen Video | bitmiş videoda yok |  |
| it only fit the opening seconds, which stay clear | es passte nur in die ersten Sekunden, und die bleiben frei | yalnızca boş kalan açılışa uyuyordu |  |
| it runs past the {creator_label_max_lines} lines a clip label holds | es ist länger als die {creator_label_max_lines} Zeilen, die ein Clip-Label fasst | klip etiketinin {creator_label_max_lines} satırına sığmıyor |  |
| its clips are already in an earlier group | seine Clips sind schon in einer früheren Gruppe | klipleri zaten önceki bir grupta |  |
| its clips are already placed at another spot | seine Clips sind schon an einer anderen Stelle | klipleri zaten başka bir yerde |  |
| its colours changed after I placed it | die Farben haben sich geändert, nachdem ich es platziert hatte | renkleri yerleştirdikten sonra değişti |  |
| its length follows your song | Die Länge richtet sich nach deinem Song | uzunluğu şarkını takip ediyor |  |
| its length follows your speech | Die Länge richtet sich nach dem, was du sagst | uzunluğu konuşmanı takip ediyor |  |
| its length follows your voiceover | Die Länge richtet sich nach deinem Voiceover | uzunluğu seslendirmeni takip ediyor |  |
| its moment was cut out | sein Moment wurde herausgeschnitten | anı kesildi |  |
| its outline no longer stands out from the fill | die Kontur hebt sich nicht mehr von der Füllung ab | kenar çizgisi artık dolgudan ayrışmıyor |  |
| label | Label | etiket |  |
| label the clips | die Clips beschriften | kliplere etiket vermek |  |
| last | zuletzt | son |  |
| last clip you added | letzten Clip, den du hinzugefügt hast | eklediğin son klip |  |
| last clip you filmed | letzten Clip, den du gefilmt hast | çektiğin son klip |  |
| leave it out | lasse ich es weg | onu çıkarayım |  |
| leave it out and end on {clip} | lasse ich es weg und beende das Video mit {clip} | onu çıkarıp videoyu {clip}te bitireyim |  |
| leave it out and start with {clip} | lasse ich es weg und beginne das Video mit {clip} | onu çıkarıp videoya {clip} ile başlayayım |  |
| letter case | Groß- und Kleinschreibung | harf biçimi |  |
| look | Look | görünüm |  |
| lyric | Songtext | şarkı sözü |  |
| make you small in a corner | dich klein in eine Ecke setzen | seni köşede küçültmek |  |
| match clips | Clips zuordnen | match clips |  |
| match song | Song auswählen | match song |  |
| middle | Mitte | orta |  |
| mix audio | Ton mischen | mix audio |  |
| montage | Montage | montage |  |
| music | Musik | müzik |  |
| music under your voiceover | Musik unter deinem Voiceover | seslendirmenin altındaki müzik |  |
| narrated | mit Voiceover | narrated |  |
| narrated planned | mit Voiceover | narrated planned |  |
| narrated ready | mit Voiceover | narrated ready |  |
| native | nativ | native |  |
| no clip got one | kein Clip hat eins bekommen | hiçbir klibe gelmedi |  |
| no room without covering a face or the captions | kein Platz, ohne ein Gesicht oder die Untertitel zu verdecken | yüzü ya da altyazıyı kapatmadan yer yoktu |  |
| no spoken moment | kein passender gesprochener Moment | konuşmanda uygun an yoktu |  |
| none of the clips carries a filming time | kein Clip hat eine Aufnahmezeit | Hiçbir klibin çekim saati yok. |  |
| none of the clips has a grounded place, landmark or creator label | kein Clip hat einen belegten Ort, eine Sehenswürdigkeit oder ein Label von dir | Hiçbir klibin yeri, mekânı ya da senin yazdığın bir etiketi yok. |  |
| none of your clips have a filming time | keiner deiner Clips hat eine Aufnahmezeit | hiçbir klibinde çekim saati yok |  |
| off | aus | kapalı |  |
| on | an | açık |  |
| on that moment ({at}s into the clip) | bei diesem Moment ({at} s im Clip) | o anda (klibin {at}. saniyesi) |  |
| on that moment ({at}s into the clip; you left the exact frame to me) | bei diesem Moment ({at} s im Clip; das genaue Bild hast du mir überlassen) | o anda (klibin {at}. saniyesi; tam kareyi bana bıraktın) |  |
| on the last frame of the last shot | auf dem letzten Bild der letzten Einstellung | son çekimin son karesinde |  |
| on the moment you picked, at its middle ({at}s into the clip) | in der Mitte des Moments, den du gewählt hast ({at} s im Clip) | seçtiğin anın ortasında (klibin {at}. saniyesi) |  |
| one clip | ein Clip | bir klip |  |
| one featured hero clip | ein hervorgehobener Haupt-Clip | öne çıkan ana klip |  |
| only one recording can be the voice that plays under the rest. | nur eine Aufnahme kann die Stimme sein, die unter den anderen läuft. | diğerlerinin altında yalnızca bir kayıt ses olarak çalabilir. |  |
| order | Reihenfolge |  |  |
| other changes | andere Änderungen | diğer değişiklikler |  |
| overlays | Einblendungen | katmanlar |  |
| photo {number} | Foto {number} | {number}. fotoğraf |  |
| photo {photos} | Foto {photos} | fotoğraf {photos} |  |
| pinned | angehefteter Text | sabit yazı |  |
| pinned text | angehefteter Text | sabit yazı |  |
| post caption | Beitragstext | paylaşım metni |  |
| put that behind you | das hinter dich legen | onu arkana koymak |  |
| put that colour behind you | diese Farbe hinter dich legen | o rengi arkana koymak |  |
| put you back full screen | dich wieder ins Vollbild setzen | seni tekrar tam ekran yapmak |  |
| queued | Warteschlange | queued |  |
| relaxed | entspannt | relaxed |  |
| remove the background behind you | den Hintergrund hinter dir entfernen | arkandaki arka planı kaldırmak |  |
| removed in the editor | im Editor entfernt | editörde kaldırıldı |  |
| render variants | Varianten erstellen | render variants |  |
| single hero | Hauptclip | single hero |  |
| slides | Folien | slides |  |
| small in a corner | klein in einer Ecke | köşede küçük |  |
| small in the bottom-left corner | klein in der unteren linken Ecke | sol alt köşede küçük |  |
| small in the bottom-right corner | klein in der unteren rechten Ecke | sağ alt köşede küçük |  |
| small in the top-left corner | klein in der oberen linken Ecke | sol üst köşede küçük |  |
| small in the top-right corner | klein in der oberen rechten Ecke | sağ üst köşede küçük |  |
| so I didn't pick a clip to end on. Tell me which clip to end on if you have one in mind. | deshalb habe ich keinen Clip als Ende ausgewählt. Sag mir, mit welchem Clip das Video enden soll, falls du einen im Kopf hast. | o yüzden sona özel bir klip koymadım. Aklında bir klip varsa hangisiyle bitireyim söyle. |  |
| so I didn't pick a clip to start with. Tell me which clip to start with if you have one in mind. | deshalb habe ich keinen Clip als Anfang ausgewählt. Sag mir, mit welchem Clip das Video beginnen soll, falls du einen im Kopf hast. | o yüzden başa özel bir klip koymadım. Aklında bir klip varsa hangisiyle başlayayım söyle. |  |
| so the video ends on the {clip}. Tell me which clip to end on if you'd like a different one. | deshalb endet das Video mit dem {clip}. Sag mir, mit welchem Clip es enden soll, wenn du einen anderen möchtest. | o yüzden video {clip}te bitiyor. Başka bir klipte bitmesini istersen hangisi olduğunu söyle. |  |
| so the video is cropped to the middle and the speaker's face can leave the frame | deshalb wird das Video auf die Mitte zugeschnitten und das Gesicht der sprechenden Person kann aus dem Bild rutschen | o yüzden video ortadan kırpıldı ve konuşan kişinin yüzü kadrajdan çıkabilir |  |
| so the video starts with the {clip}. Tell me which clip to start with if you'd like a different one. | deshalb beginnt das Video mit dem {clip}. Sag mir, mit welchem Clip es beginnen soll, wenn du einen anderen möchtest. | o yüzden video {clip} ile başlıyor. Başka bir klip ile başlamasını istersen hangisi olduğunu söyle. |  |
| so the whole frame shows with black bars above and below | deshalb ist das ganze Bild mit schwarzen Balken oben und unten zu sehen | o yüzden görüntünün tamamı üstte ve altta siyah bantlarla görünüyor |  |
| sound effects | Soundeffekte | ses efektleri |  |
| start with {first} | mit {first} beginnen | {first} ile başla |  |
| start with {first}; end with {last} | mit {first} beginnen; mit {last} enden | {first} ile başla; {last} ile bitir |  |
| starting full screen isn't available on iPhone yet | im Vollbild zu starten ist auf dem iPhone noch nicht möglich | tam ekran başlamak iPhone'da henüz yok |  |
| subtitled | mit Untertiteln | subtitled |  |
| talking head | Talking Head | talking head |  |
| tell me to mute it and I'll take it out | sag mir, dass ich ihn stummschalten soll, dann nehme ich ihn raus | kapatmamı söylersen çıkarırım |  |
| text | Text | yazı |  |
| text explainer | Text-Erklärvideo | text explainer |  |
| text for {what} | Text für {what} | {what} için yazı |  |
| that clip | dieser Clip | o klip |  |
| that isn't something I can change in this edit yet | das kann ich in diesem Edit noch nicht ändern | bu düzenlemede bunu henüz değiştiremiyorum |  |
| that part | dieser Teil | o bölüm |  |
| that part is about {kept}s | dieser Teil ist etwa {kept} s lang | o kısım yaklaşık {kept} saniye |  |
| that word | dieses Wort | o kelimeyi |  |
| that would only change sections you did not flag | das würde nur Abschnitte ändern, die du nicht markiert hast | bu yalnızca işaretlemediğin bölümleri değiştirirdi |  |
| the arrow next to your ending ("{req_literal}") | der Pfeil neben deinem Ende ("{req_literal}") | kapanış yazının yanındaki ok ("{req_literal}") |  |
| the caption style | der Untertitel-Stil | altyazı stili |  |
| the captions | die Untertitel | altyazılar |  |
| the clip label position | die Position der Clip-Labels | klip etiketi konumu |  |
| the clips can't each be held long enough for it within the length you asked for | In der gewünschten Länge kann ich nicht jeden Clip lange genug dafür zeigen | klipler, istediğin süre içinde bunun için yeterince uzun tutulamıyor |  |
| the clips did not end up there | die Clips sind dort nicht gelandet | klipler oraya gelmedi |  |
| the clips' own spoken lines are kept whole, which only the fast-montage layout guarantees | Die gesprochenen Sätze in den Clips bleiben ganz, und das garantiert nur das Layout der schnellen Montage | kliplerin kendi konuşmaları bütün tutuluyor, bunu sadece hızlı montaj düzeni garanti ediyor |  |
| the closing badge | das Abschluss-Badge | kapanış rozeti |  |
| the closing title | der Abschlusstitel | kapanış başlığı |  |
| the closing visual | das Abschluss-Visual | kapanış görseli |  |
| the complete all-selection can no longer be verified; refresh and try again | die vollständige "Alle"-Auswahl lässt sich nicht mehr prüfen; aktualisiere und versuch es nochmal | "Hepsi" seçiminin tamamı artık doğrulanamıyor; yenile ve tekrar dene. |  |
| the complete all-selection changed after Kria read it; refresh and try again | die vollständige "Alle"-Auswahl hat sich geändert, nachdem Kria sie gelesen hat; aktualisiere und versuch es nochmal | Kria okuduktan sonra "hepsi" seçimi değişti; yenile ve tekrar dene. |  |
| the cuts between spoken lines keep a fixed length | Die Schnitte zwischen den gesprochenen Sätzen haben eine feste Länge | konuşmalar arasındaki kesimler sabit uzunlukta |  |
| the cuts follow a fast pace | Die Schnitte folgen einem schnellen Tempo | kesimler hızlı tempoda |  |
| the cuts follow your voiceover | Die Schnitte folgen deinem Voiceover | kesimler seslendirmeni izliyor |  |
| the cuts keep their usual length | Die Schnitte behalten ihre übliche Länge | kesimler her zamanki uzunluğunda |  |
| the edit changed since I looked, so I skipped it | der Edit hat sich geändert, seit ich nachgesehen habe, deshalb habe ich es übersprungen | ben bakarken düzenleme değişti, bu yüzden atladım |  |
| the exact voiceover plan | der genaue Voiceover-Plan | seslendirme planın |  |
| the first clip you added | dem ersten Clip, den du hinzugefügt hast | eklediğin ilk klip |  |
| the first clip you filmed | dem ersten Clip, den du gefilmt hast | çektiğin ilk klip |  |
| the footage brightness change | die Helligkeitsänderung des Materials | çekimin parlaklık değişikliği |  |
| the footage doesn't fit that layout | Das Material passt nicht zu diesem Layout | görüntüler bu düzene uymuyor |  |
| the guided editor revision changed after Kria read it; refresh and try again | die Version des geführten Editors hat sich geändert, nachdem Kria sie gelesen hat; aktualisiere und versuch es nochmal | Kria okuduktan sonra editördeki düzenleme sürümü değişti; yenile ve tekrar dene. |  |
| the last clip you added | dem letzten Clip, den du hinzugefügt hast | eklediğin son klip |  |
| the last clip you filmed | dem letzten Clip, den du gefilmt hast | çektiğin son klip |  |
| the length you asked for | die gewünschte Länge | istediğin uzunluk |  |
| the music | die Musik | müzik |  |
| the opening hook idea | die Idee für den Einstiegs-Hook | açılış kanca fikri |  |
| the opening title | der Anfangstitel | açılış başlığı |  |
| the order you described | die Reihenfolge, die du beschrieben hast | tarif ettiğin sıra |  |
| the overlay display | die geplante Einblendungs-Anzeige | katman gösterimi |  |
| the photo | das Foto | fotoğraf |  |
| the photo and video timing | das Timing von Foto und Video | fotoğraf ve video zamanlaman |  |
| the plan has no story outline to build chapters from | Der Plan hat keine Story-Gliederung, aus der ich Kapitel bauen könnte | bölümleri oluşturacak bir hikâye taslağı yok |  |
| the planned direction | die geplante Richtung | planlanan yön |  |
| the planned music or voiceover | die geplante Musik oder das geplante Voiceover | planlanan müzik ya da seslendirme |  |
| the planned pacing | das geplante Tempo | planlanan tempo |  |
| the planned story structure | die geplante Story-Struktur | planlanan hikâye yapısı |  |
| the planned text color | die geplante Textfarbe | planlanan yazı rengi |  |
| the planned text font | die geplante Textschrift | planlanan yazı tipi |  |
| the same clip plays more than once, which only the fast-montage layout allows | Derselbe Clip läuft mehr als einmal, und das erlaubt nur das Layout der schnellen Montage | aynı klip birden fazla kez oynuyor, buna sadece hızlı montaj düzeni izin veriyor |  |
| the saved all-selection proof is incomplete; refresh and try again | der gespeicherte Nachweis der "Alle"-Auswahl ist unvollständig; aktualisiere und versuch es nochmal | Kaydedilen "hepsi" seçimi eksik; yenile ve tekrar dene. |  |
| the sentence {clip} belongs to ends in the first seconds, so it plays after the next one | der Satz, zu dem {clip} gehört, endet in den ersten Sekunden, deshalb läuft er nach dem nächsten | {clip} için ilgili cümle ilk saniyelerde bitiyor, o yüzden bir sonrakinden sonra oynuyor |  |
| the sound effects | die Soundeffekte | ses efektleri |  |
| the source pool changed after Kria read it; refresh and try again | die Quellen haben sich geändert, nachdem Kria sie gelesen hat; aktualisiere und versuch es nochmal | Kria okuduktan sonra kaynaklar değişti; yenile ve tekrar dene. |  |
| the story archetype | der Story-Typ | hikâye kalıbı |  |
| the story text couldn't be placed on it | Der Story-Text ließ sich nicht darauf platzieren | hikâye yazısı buna yerleştirilemedi |  |
| the talking clip is {take}s | der Clip, in dem du sprichst, ist {take} s lang | konuşma klibi {take} saniye |  |
| the text animation | die Text-Animation | yazı animasyonunu |  |
| the text will animate in as it appears; I'll check it in the finished video | der Text wird beim Erscheinen animiert eingeblendet; ich prüfe das im fertigen Video | yazı belirirken animasyonlu girecek; bitmiş videoda kontrol edeceğim |  |
| the text will play the {label} entrance; I'll check it in the finished video | der Text wird mit der Eingangsanimation {label} abgespielt; ich prüfe das im fertigen Video | yazı {label} girişiyle oynayacak; bitmiş videoda kontrol edeceğim |  |
| the time you gave for {clip} is past the end of your talk, so it plays at the very end | die Zeit, die du für {clip} genannt hast, liegt nach dem Ende deiner Rede, deshalb läuft er ganz am Ende | {clip} için verdiğin zaman konuşmanın bitişinden sonra, o yüzden en sonda oynuyor |  |
| the title | der Titel | başlık |  |
| the title animation | die Titel-Animation | başlık animasyonu |  |
| the treatments | die Effekte | efektler |  |
| the value wasn't one I can apply | diesen Wert kann ich nicht anwenden | bu değeri uygulayamıyorum |  |
| the video | das Video | video |  |
| the video follows your speech | Das Video folgt dem, was du sagst | video konuşmanı izliyor |  |
| the video follows your voiceover | Das Video folgt deinem Voiceover | video seslendirmeni izliyor |  |
| the visuals timed to your words | die Visuals, die auf deine Worte abgestimmt sind | sözlerine göre zamanlanan görseller |  |
| the {name} clip | der Clip {name} | {name} klibi |  |
| the {sorted_names_0} animation | die Animation {sorted_names_0} | {sorted_names_0} animasyonunu |  |
| the {what} name on its clips | der Name {what} auf seinen Clips | kliplerindeki {what} adı |  |
| the {word} isn't your exact wording | {word} entspricht nicht genau deinem Wortlaut | {word} tam olarak yazdığın gibi değil |  |
| the {word} runs past the end of the video | {word} läuft über das Ende des Videos hinaus | {word} videonun sonundan taşıyor |  |
| then | danach | sonra |  |
| there are too many clips to lay out as story chapters | Es sind zu viele Clips, um sie als Story-Kapitel anzuordnen | bölümlere ayırmak için çok fazla klip var |  |
| these aren't where you asked: {join_facts_sequence_unmet} | diese liegen nicht dort, wo du sie wolltest: {join_facts_sequence_unmet} | bunlar istediğin yerde değil: {join_facts_sequence_unmet} |  |
| they'll be in {language} | sie sind auf {language} | altyazılar {language} olacak |  |
| they'll be in {language}, and I'll check that in the finished video | sie sind auf {language}, und das prüfe ich im fertigen Video | altyazılar {language} olacak, bunu bitmiş videoda kontrol edeceğim |  |
| this edit has no control to turn it down yet | in diesem Edit gibt es noch keine Einstellung, um ihn leiser zu machen | bu düzenlemede onu kısacak bir ayar henüz yok |  |
| this editor can only restage it as a fast montage, so nothing changed. A different direction needs a fresh edit | Dieser Editor kann es nur als schnelle Montage neu aufbauen, deshalb hat sich nichts geändert. Für eine andere Richtung brauchst du einen neuen Edit | bu editör onu yalnızca hızlı bir montaj olarak yeniden kurabiliyor, o yüzden hiçbir şey değişmedi. Başka bir yön için yeni bir düzenleme gerekiyor |  |
| title | Titel | başlık |  |
| top | oben | üst |  |
| top left | oben links | sol üst |  |
| top right | oben rechts | sağ üst |  |
| turn it down in the editor under Sounds > Original audio, or tell me to mute it | stell ihn im Editor unter „Sounds > Original audio“ leiser oder sag mir, dass ich ihn stummschalten soll | editörde Sesler > Orijinal ses bölümünden kıs ya da bana kapatmamı söyle |  |
| update the Kria app to start full screen | aktualisiere die Kria-App, um im Vollbild zu starten | tam ekran başlamak için Kria uygulamasını güncelle |  |
| upload | Hochladen | upload |  |
| words light up as they're spoken | Wörter leuchten auf, während sie gesprochen werden | kelimeler söylendikçe yanıyor |  |
| write out the words to spell and I'll spell them your way | schreib die Wörter auf, auf die ich bei der Schreibweise achten soll, dann schreibe ich sie so, wie du es willst | yazımına dikkat edeceğim kelimeleri tek tek yaz, senin yazdığın gibi yazayım |  |
| you asked for a freeze frame or a speed change, which only the fast-montage layout plays | Du wolltest ein Standbild oder ein anderes Tempo, und das spielt nur das Layout der schnellen Montage ab | dondurulmuş kare ya da hız değişimi istedin, bunu sadece hızlı montaj düzeni oynatıyor |  |
| you asked for curved text, which only the fast-montage layout draws | Du wolltest gebogenen Text, und den zeichnet nur das Layout der schnellen Montage | kavisli yazı istedin, bunu sadece hızlı montaj düzeni çiziyor |  |
| you asked for text on individual clips, which only the fast-montage layout places | Du wolltest Text auf einzelnen Clips, und den setzt nur das Layout der schnellen Montage | tek tek kliplere yazı istedin, bunu sadece hızlı montaj düzeni yerleştirebiliyor |  |
| you asked me to keep a part of a clip out, which only the fast-montage layout can guarantee | Du wolltest, dass ich einen Teil eines Clips weglasse, und das kann nur das Layout der schnellen Montage garantieren | bir klibin bir bölümünü dışarıda tutmamı istedin, bunu sadece hızlı montaj düzeni garanti edebiliyor |  |
| you chose grouping first, each group in the order you filmed it | du hast zuerst die Gruppierung gewählt, jede Gruppe in der Reihenfolge, in der du sie gefilmt hast | önce gruplamayı seçtin, her grup çektiğin sırada |  |
| you chose grouping first, so it's in filming order inside each group | du hast zuerst die Gruppierung gewählt, deshalb liegen sie innerhalb jeder Gruppe in Aufnahmereihenfolge | önce gruplamayı seçtin, o yüzden her grubun içinde çekim sırasında |  |
| you chose strictly chronological order, so· | du hast eine streng chronologische Reihenfolge gewählt, deshalb gilt:· | kesin kronolojik sırayı seçtin, o yüzden· |  |
| you didn't say what order its clips play in | du hast nicht gesagt, in welcher Reihenfolge seine Clips laufen | kliplerinin hangi sırayla oynayacağını söylemedin |  |
| you don't say it that many times | so oft sagst du es nicht | bunu o kadar çok kez söylemiyorsun |  |
| you let me pick this moment, so I chose it myself | du hast mir diesen Moment überlassen, also habe ich ihn selbst gewählt | bu anı seçmeyi bana bıraktın, ben seçtim |  |
| you named exact moments, which only the fast-montage layout plays as given | Du hast genaue Momente genannt, und die spielt nur das Layout der schnellen Montage genau so ab | belirli anları sen seçtin, bunları sadece hızlı montaj düzeni olduğu gibi oynatıyor |  |
| you say it in more than one take, so I left it in; tell me which take to cut it from | du sagst es in mehr als einem Take, deshalb habe ich es drin gelassen; sag mir, aus welchem Take ich es schneiden soll | bunu birden fazla çekimde söylüyorsun, o yüzden dokunmadım; hangi çekimden keseyim söyle |  |
| you say it more than once, so tell me which one to cut: the first, second or last | du sagst es mehr als einmal, also sag mir, welches ich schneiden soll: das erste, das zweite oder das letzte | bunu birden fazla kez söylüyorsun, hangisini keseyim söyle: ilkini, ikincisini ya da sonuncusunu |  |
| you set exact timing for photos or cuts, which only the fast-montage layout keeps | Du hast genaue Zeiten für Fotos oder Schnitte festgelegt, und die hält nur das Layout der schnellen Montage ein | fotoğraflar ya da kesimler için net süre verdin, bunu sadece hızlı montaj düzeni koruyor |  |
| you told me this moment isn't in your clips | du hast mir gesagt, dass dieser Moment nicht in deinen Clips ist | bu anın kliplerinde olmadığını söyledin |  |
| your clip instructions | deine Clip-Anweisungen | klip talimatların |  |
| your clip labels | deine Clip-Labels | klip etiketlerin |  |
| your closing title | dein Abschlusstitel | kapanış başlığın |  |
| your iPhone build can't draw the crossfades that layout uses yet | Deine iPhone-Version kann die Überblendungen dieses Layouts noch nicht zeichnen | iPhone sürümün o düzenin kullandığı geçişleri henüz çizemiyor |  |
| your last change | deine letzte Änderung | son değişikliğin |  |
| your no-captions choice | deine Entscheidung gegen Untertitel | altyazısız isteğin |  |
| your on-screen texts | deine Texte auf dem Bildschirm | ekran yazıların |  |
| your opening title | dein Anfangstitel | açılış başlığın |  |
| your own song sets the cut points, which only the fast-montage layout follows | Dein eigener Song bestimmt die Schnittpunkte, und denen folgt nur das Layout der schnellen Montage | kendi şarkın kesim noktalarını belirliyor, bunu sadece hızlı montaj düzeni izliyor |  |
| your phone can't draw that layout for this footage | Dein Handy kann dieses Layout für dieses Material nicht zeichnen | telefonun bu görüntüler için o düzeni çizemiyor |  |
| your song sets where the cuts fall | Dein Song bestimmt, wo die Schnitte liegen | kesimlerin yerini şarkın belirliyor |  |
| your take has no word timings to find it in | dein Take hat keine Wortzeiten, in denen ich es finden könnte | çekiminde onu bulacak kelime zamanlaması yok |  |
| your take is too short to start full screen and still move into the corner | dein Take ist zu kurz, um im Vollbild zu starten und dann noch in die Ecke zu wechseln | çekimin tam ekran başlayıp köşeye geçmek için fazla kısa |  |
| your take is {take}s | dein Take ist {take} s lang | çekimin {take} saniye |  |
| your voice can't fill the whole video. | deine Stimme kann nicht das ganze Video füllen. | sesin videonun tamamını dolduramıyor. |  |
| your {names} clips were filmed mixed together, so I can't do both. | deine {names}-Clips wurden durcheinander gefilmt, deshalb kann ich nicht beides machen. | {names} klipleri birbirine karışık çekilmiş, bu yüzden ikisini birden yapamıyorum. |  |
| {and_names_absent} doesn't appear in the captions, so I couldn't check its spelling. | {and_names_absent} kommt in den Untertiteln nicht vor, deshalb konnte ich die Schreibweise nicht prüfen. | Altyazılarda {and_names_absent} geçmiyor, o yüzden yazımını kontrol edemedim. |  |
| {and_names_absent} don't appear in the captions, so I couldn't check their spelling. | {and_names_absent} kommen in den Untertiteln nicht vor, deshalb konnte ich die Schreibweise nicht prüfen. | Altyazılarda {and_names_absent} geçmiyor, o yüzden yazımlarını kontrol edemedim. |  |
| {and_names_spelled} are spelled as you wrote them. | {and_names_spelled} sind so geschrieben, wie du sie geschrieben hast. | {and_names_spelled} yazdığın gibi yazılmış. |  |
| {and_names_spelled} is spelled as you wrote it. | {and_names_spelled} ist so geschrieben, wie du es geschrieben hast. | {and_names_spelled} yazdığın gibi yazılmış. |  |
| {asked_s} seconds | {asked_s} Sekunden | {asked_s} saniye |  |
| {asked} seconds | {asked} Sekunden | {asked} saniye |  |
| {asked}s | {asked} s | {asked} saniye |  |
| {at} is past the end of the video ({end:.1f} s), so the small card of {name} isn't shown | {at} liegt nach dem Ende des Videos ({end:.1f} s), deshalb wird die kleine Karte von {name} nicht gezeigt | {at} videonun sonundan sonra ({end:.1f} sn), o yüzden {name} küçük kartı gösterilmiyor |  |
| {at} is past the end of the video, so the small card of {name} isn't shown | {at} liegt nach dem Ende des Videos, deshalb wird die kleine Karte von {name} nicht gezeigt | {at} videonun sonundan sonra, o yüzden {name} küçük kartı gösterilmiyor |  |
| {candidate_intro} Type the words you want and I'll use them exactly, or reply "{options_0_label}". | {candidate_intro} Schreib die Wörter, die du möchtest, und ich nehme sie genau so, oder antworte mit "{options_0_label}". | {candidate_intro} İstediğin kelimeleri yaz, aynen kullanırım; ya da "{options_0_label}" yaz. |  |
| {candidate_intro} Unfortunately {after_connector_candidate_reason} The most I can do is: {only_label}. Reply "{only_label}" to go with that, or change your request. | {candidate_intro} Leider gibt es einen Haken: {after_connector_candidate_reason} Das Beste, was ich machen kann: {only_label}. Antworte mit "{only_label}", wenn das für dich passt, oder ändere deine Anfrage. | {candidate_intro} Maalesef {after_connector_candidate_reason} Yapabileceğim en fazla şu: {only_label}. Bunu istiyorsan "{only_label}" yaz ya da isteğini değiştir. |  |
| {candidate_intro} Unfortunately {after_connector_candidate_reason} Which do you prefer? | {candidate_intro} Leider gibt es einen Haken: {after_connector_candidate_reason} Was ist dir lieber? | {candidate_intro} Maalesef {after_connector_candidate_reason} Hangisini tercih edersin? |  |
| {candidate_intro} {candidate_reason} I can make it {only_label}. Reply "{only_label}" to go with that, or change your request. | {candidate_intro} {candidate_reason} Ich kann es so machen: {only_label}. Antworte mit "{only_label}", wenn das für dich passt, oder ändere deine Anfrage. | {candidate_intro} {candidate_reason} Şunu yapabilirim: {only_label}. Bunu istiyorsan "{only_label}" yaz ya da isteğini değiştir. |  |
| {clip_plays_on_met}, but I can't hear "{quote}" in it yet | {clip_plays_on_met}, aber ich kann "{quote}" darin noch nicht hören | {v1}, ama "{quote}" sözünü henüz duyamıyorum |  |
| {clip_plays_on_met}, so "{line}" is heard | {clip_plays_on_met}, deshalb ist "{line}" zu hören | {v1}, böylece "{line}" duyuluyor |  |
| {clip} cuts into your talk in the middle of a word, as you asked | {clip} schneidet mitten in einem Wort in deine Rede, wie du es wolltest | {clip} konuşmanı bir kelimenin ortasında kesiyor, istediğin gibi |  |
| {clip} plays at the very end of your talk, as you asked | {clip} läuft ganz am Ende deiner Rede, wie du es wolltest | {clip} konuşmanın en sonunda oynuyor, istediğin gibi |  |
| {clip} plays in the first seconds, as you asked | {clip} läuft in den ersten Sekunden, wie du es wolltest | {clip} ilk saniyelerde oynuyor, istediğin gibi |  |
| {clip} plays right after another clip about the same part | {clip} läuft direkt nach einem anderen Clip zum selben Teil | {clip}, aynı kısımla ilgili başka bir klibin hemen ardından oynuyor |  |
| {clip} plays right next to another, as you asked | {clip} läuft direkt neben einem anderen, wie du es wolltest | {clip} başka bir klibin hemen yanında oynuyor, istediğin gibi |  |
| {conflict_intro} {conflict_reason} I won't guess, so I haven't made an edit yet. To go ahead, reply {ways}.{unchanged} | {conflict_intro} {conflict_reason} Ich rate nicht, deshalb habe ich noch nichts geschnitten. Um weiterzumachen, antworte mit {ways}.{unchanged} | {conflict_intro} {conflict_reason} Tahmin yürütmeyeceğim, o yüzden henüz bir düzenleme yapmadım. Devam etmek için {ways_tr}.{unchanged} |  |
| {count} Visuals | {count} Visuals | {count} görsel |  |
| {count} camera effect | {count} Kameraeffekt | {count} {noun_tr} |  |
| {count} camera effects | {count} Kameraeffekte | {count} {noun_tr} |  |
| {count} caption | {count} Untertitel | {count} {noun_tr} |  |
| {count} captions | {count} Untertitel | {count} {noun_tr} |  |
| {count} clip | {count} Clip | {count} {noun_tr} |  |
| {count} clips | {count} Clips | {count} {noun_tr} |  |
| {count} clips can't each stay on screen long enough to be seen in {asked} seconds (each needs about {floor_s:g} seconds). | {count} Clips können in {asked} Sekunden nicht jeweils lang genug zu sehen sein (jeder braucht etwa {floor_s:g} Sekunden). | {asked} saniyede {count} klibin her biri görülecek kadar ekranda kalamaz (her birine yaklaşık {floor_s:g} saniye lazım). |  |
| {count} clips need at least {clean_seconds_lo_s} seconds to each be seen. | {count} Clips brauchen mindestens {clean_seconds_lo_s} Sekunden, damit jeder zu sehen ist. | {count} klibin her birinin görülmesi için en az {clean_seconds_lo_s} saniye gerekir. |  |
| {count} filler sound | {count} Füllgeräusch | {count} dolgu sesi |  |
| {count} filler sounds | {count} Füllgeräusche | {count} dolgu sesi |  |
| {count} label | {count} Label | {count} {noun_tr} |  |
| {count} labels | {count} Labels | {count} {noun_tr} |  |
| {count} long pause | {count} lange Pause | {count} uzun duraklama |  |
| {count} long pauses | {count} lange Pausen | {count} uzun duraklama |  |
| {count} of your clips show "{label}". Which one should I use for it? Keep that one ticked and untick the others. | "{label}" ist in {count} deiner Clips zu sehen. Welchen soll ich dafür nehmen? Lass ihn angehakt und nimm bei den anderen den Haken raus. | "{label}" {count} klibinde görünüyor. Hangisini kullanayım? Onu işaretli bırak, diğerlerinin işaretini kaldır. |  |
| {count} pause | {count} Pause | {count} duraklama |  |
| {count} pauses | {count} Pausen | {count} duraklama |  |
| {count} picture | {count} Bild | {count} {noun_tr} |  |
| {count} pictures | {count} Bilder | {count} {noun_tr} |  |
| {count} restarted sentence | {count} neu begonnener Satz | {count} baştan başlanan cümle |  |
| {count} restarted sentences | {count} neu begonnene Sätze | {count} baştan başlanan cümle |  |
| {count} sound effect | {count} Soundeffekt | {count} {noun_tr} |  |
| {count} sound effects | {count} Soundeffekte | {count} {noun_tr} |  |
| {count} take has no usable singing or words, so it is in as short muted clip. Trim or remove it in the editor. | {count} Take enthält keinen brauchbaren Gesang und keine brauchbaren Worte, deshalb habe ich ihn als kurzen stummen Clip eingefügt. Kürze oder entferne ihn im Editor. | {count} çekimde kullanılabilir şarkı ya da söz yok, bu yüzden kısa ve sessiz klip olarak eklendi. Editörde kısaltabilir veya kaldırabilirsin. |  |
| {count} take is placed by my best guess and may be slightly off; check it in the editor. | {count} Take habe ich nach bester Schätzung platziert, er könnte etwas danebenliegen; prüf ihn im Editor. | {count} çekimi tahminime göre yerleştirdim, biraz kayık olabilir; editörde kontrol et. |  |
| {count} take sits later in the song than a 2-minute video can hold. | {count} Take liegt so spät im Song, dass er nicht mehr in ein 2-Minuten-Video passt. | {count} çekim şarkıda, 2 dakikalık bir videonun sığabileceğinden daha geride kalıyor. |  |
| {count} takes are placed by my best guess and may be slightly off; check them in the editor. | {count} Takes habe ich nach bester Schätzung platziert, sie könnten etwas danebenliegen; prüf sie im Editor. | {count} çekimi tahminime göre yerleştirdim, biraz kayık olabilir; editörde kontrol et. |  |
| {count} takes have no usable singing or words, so they are in as short muted clips. Trim or remove them in the editor. | {count} Takes enthalten keinen brauchbaren Gesang und keine brauchbaren Worte, deshalb habe ich sie als kurze stumme Clips eingefügt. Kürze oder entferne sie im Editor. | {count} çekimde kullanılabilir şarkı ya da söz yok, bu yüzden kısa ve sessiz klip olarak eklendi. Editörde kısaltabilir veya kaldırabilirsin. |  |
| {count} takes sit later in the song than a 2-minute video can hold. | {count} Takes liegen so spät im Song, dass sie nicht mehr in ein 2-Minuten-Video passen. | {count} çekim şarkıda, 2 dakikalık bir videonun sığabileceğinden daha geride kalıyor. |  |
| {count} text | {count} Text | {count} {noun_tr} |  |
| {count} texts | {count} Texte | {count} {noun_tr} |  |
| {detail} Your complete request is saved and your draft is unchanged. {follow_up} | {detail} Deine komplette Anfrage ist gespeichert und dein Entwurf ist unverändert. {follow_up} | {detail} Tüm isteğin kaydedildi ve taslağın değişmedi. {follow_up} |  |
| {edit_format} format | Format: {edit_format} | Format: {v1} |  |
| {head} and {v1} | {head} und {v1} | {head} ve {v1} |  |
| {head}, so I left out "{literal}". Tell me which clip it goes on if you still want it. | {head}, deshalb habe ich "{literal}" weggelassen. Sag mir, auf welchen Clip es soll, falls du es trotzdem willst. | {head}, o yüzden "{literal}" yazısını koymadım. Yine de istersen hangi klibe gideceğini söyle. |  |
| {hidden} Visuals | {hidden} Visuals | {hidden} görsel |  |
| {label_0} {before}{level_first_after}{unit} | {label_0} {before}{level_first_after}{unit} | {label_1} {before}{level_first_after}{unit} |  |
| {label} clips play from {start:.1f} s to {end:.1f} s, under: "{excerpt}" | {label}: Die Clips laufen von {start:.1f} s bis {end:.1f} s, während du sagst: „{excerpt}“ | {tr_label} klipleri {start:.1f} sn ile {end:.1f} sn arasında oynuyor, şu sözlerle: "{excerpt}" |  |
| {label} outlines {outline} in future videos. | {label}: Kontur {outline} in künftigen Videos. | Gelecek videolarda {role} için kenar çizgisi {outline} olsun. |  |
| {label} shadows {state} in future videos. | {label}: Schatten {state} in künftigen Videos. | Gelecek videolarda {role} için gölge {state} olsun. |  |
| {left_out} more of your labels can't be clip labels either. | {left_out} weitere deiner Labels können auch keine Clip-Labels sein. | {left_out} etiketin daha klip etiketi olamıyor. |  |
| {len_bad} of {len_facts_per_clip_text} labels still show more than the hour. A re-render can't reformat them; ask me again to change them in the editor. | {len_bad} von {len_facts_per_clip_text} Labels zeigen noch mehr als die Uhrzeit. Ein neues Erstellen des Videos formatiert sie nicht um; bitte mich nochmal, sie im Editor zu ändern. | {len_facts_per_clip_text} etiketin {len_bad} tanesi hâlâ saatten fazlasını gösteriyor. Videoyu yeniden oluşturmak bunları düzeltmez; editörde değiştirmem için tekrar iste. |  |
| {len_lengths} moments, the longest {longest:.1f} s | {len_lengths} Momente, der längste {longest:.1f} s | {len_lengths} an, en uzunu {longest:.1f} sn |  |
| {len_missing} of your {many} clips have no filming time | {len_missing} deiner {many} Clips haben keine Aufnahmezeit | {many} klibinden {len_missing} tanesinde çekim saati yok |  |
| {len_off} of {len_rows} clips aren't {seconds:g}s ({shown}) | {len_off} von {len_rows} Clips sind nicht {seconds:g} s lang ({shown}) | {len_rows} klipten {len_off} tanesi {seconds:g} sn değil ({shown}) |  |
| {len_off} of {len_rows} texts don't have the requested {names} yet | Bei {len_off} von {len_rows} Texten fehlt noch der gewünschte Stil ({names}) | {len_rows} metinden {len_off} tanesinde istenen {names} henüz yok |  |
| {len_off} of {total} text didn't end up reading exactly “{req_literal}”. | Bei {len_off} von {total} Text steht am Ende nicht genau „{req_literal}“. | {total} yazının {len_off} tanesi tam olarak “{req_literal}” olmadı. |  |
| {len_off} of {total} texts didn't end up reading exactly “{req_literal}”. | Bei {len_off} von {total} Texten steht am Ende nicht genau „{req_literal}“. | {total} yazının {len_off} tanesi tam olarak “{req_literal}” olmadı. |  |
| {len_ok} of {len_rows} of those clips play in your talk:· | {len_ok} von {len_rows} dieser Clips laufen in deiner Rede:· | O kliplerin {len_rows} taneden {len_ok} tanesi konuşmanda oynuyor:· |  |
| {missed} aren't where you asked. | {missed} sind nicht dort, wo du sie wolltest. | {missed} istediğin yerde değil. |  |
| {missed} isn't where you asked. | {missed} ist nicht dort, wo du es wolltest. | {missed} istediğin yerde değil. |  |
| {more} more | {more} weitere | {more} tane daha |  |
| {names_overlap} would have landed on another pop-in at the same moment | {names_overlap} wäre im selben Moment auf einer anderen Einblendung gelandet | {names_overlap} aynı anda başka bir görselin üstüne denk gelirdi |  |
| {names} has no filming time, so I can't put it in the order you filmed. | Bei {names} fehlt die Aufnahmezeit, deshalb kann ich nicht nach der Reihenfolge deiner Aufnahmen sortieren. | {names_tr} için çekim saati yok, o yüzden onu çektiğin sıraya koyamıyorum. |  |
| {names} have no filming time, so I can't put them in the order you filmed. | Bei {names} fehlt die Aufnahmezeit, deshalb kann ich sie nicht in die Reihenfolge deiner Aufnahmen bringen. | {names_tr} için çekim saati yok, o yüzden onları çektiğin sıraya koyamıyorum. |  |
| {names} shown above your title | {names} über deinem Titel gezeigt | {names} başlığının üstünde çıkıyor |  |
| {name} is only {seconds_round_length} seconds long, so leaving out {seconds_intent_part_s} seconds leaves nothing of it to show. Should I leave the whole clip out? | {name} ist nur {seconds_round_length} Sekunden lang, wenn ich {seconds_intent_part_s} Sekunden weglasse, bleibt nichts zum Zeigen übrig. Soll ich den ganzen Clip weglassen? | {name} yalnızca {seconds_round_length} saniye; {seconds_intent_part_s} saniyesini çıkarınca gösterecek bir şey kalmıyor. Klibin tamamını çıkarayım mı? |  |
| {name} isn't above your title in this video. | {name} ist in diesem Video nicht über deinem Titel. | {name} bu videoda başlığının üstünde değil. |  |
| {note} (You left it to me, so I went with it.) | {note} (Du hast es mir überlassen, deshalb habe ich das genommen.) | {note} (Kararı bana bıraktığın için bunu seçtim.) |  |
| {n} clip had no capture time, so I kept its attachment order. | {n} Clip hatte keine Aufnahmezeit, deshalb habe ich ihn an der Stelle gelassen, an der du ihn hinzugefügt hast. | {n} klibin çekim zamanı yoktu, o yüzden bunları eklediğin sırada bıraktım. |  |
| {n} clip is too short for its text to stay on screen long enough to read. | {n} Clip ist zu kurz, als dass sein Text lange genug zum Lesen zu sehen wäre. | {n} klip, yazısının okunacak kadar ekranda kalması için çok kısa. |  |
| {n} clips are too short for its text to stay on screen long enough to read. | {n} Clips sind zu kurz, als dass ihr Text lange genug zum Lesen zu sehen wäre. | {n} klip, yazısının okunacak kadar ekranda kalması için çok kısa. |  |
| {n} clips had no capture time, so I kept their attachment order. | {n} Clips hatten keine Aufnahmezeit, deshalb habe ich sie in der Reihenfolge gelassen, in der du sie hinzugefügt hast. | {n} klibin çekim zamanı yoktu, o yüzden bunları eklediğin sırada bıraktım. |  |
| {n} sideways clip | {n} Querformat-Clip | {n} yatay klip |  |
| {n} sideways clips | {n} Querformat-Clips | {n} yatay klip |  |
| {pacing} pacing | Tempo: {pacing} | Tempo: {v1} |  |
| {phrase} for {where} | {phrase} ({where}) | {phrase} ({where}) |  |
| {phrase}, but that isn't what you asked for | {phrase}, aber das ist nicht, was du wolltest | {phrase}, ama istediğin bu değil |  |
| {prefix} around {times} in your clip | {prefix} um {times} in deinem Clip | {prefix} (klibinde yaklaşık {times}) |  |
| {prefix} at {times} in the video | {prefix} bei {times} im Video | {prefix} (videoda {times}) |  |
| {prefix}, “{text}”, around {times} in your clip | {prefix}, „{text}“, um {times} in deinem Clip | {prefix}: “{text}” (klibinde yaklaşık {times}) |  |
| {prefix}, “{text}”, at {times} in the video | {prefix}, „{text}“, bei {times} im Video | {prefix}: “{text}” (videoda {times}) |  |
| {prefix}; {tail} and you asked for {asked} | {prefix}; {tail}, und du wolltest {asked} | {v1}; {tr_tail}, sen {asked_tr} istedin |  |
| {quoted_text} can't be a clip label: {reasons}. | {quoted_text} kann kein Clip-Label sein: {reasons}. | {quoted_text} klip etiketi olamaz: {reasons}. |  |
| {quotes} is in the cut, but the clips' own sound is off | {quotes} ist im Schnitt, aber der eigene Ton der Clips ist aus | {quotes} kesitte var ama kliplerin kendi sesi kapalı |  |
| {quotes} plays whole, in the clip's own sound | {quotes} läuft komplett, im eigenen Ton des Clips | {quotes} baştan sona, klibin kendi sesiyle çalıyor |  |
| {reason}; you didn't say how fast, so I used {speed} | {reason}; du hast nicht gesagt, wie schnell, deshalb habe ich {speed} genommen | {reason}; ne kadar hızlı söylemediğin için {speed} kullandım |  |
| {refusal_0}.· | {refusal_0}.· | {refusal_1}.· |  |
| {req_description} (none of your clips show {missing}) | {req_description} (in keinem deiner Clips ist {missing} zu sehen) | {req_description} (kliplerinde {missing} yok) |  |
| {req_description} (without {missing}) | {req_description} (ohne {missing}) | {req_description} ({missing} olmadan) |  |
| {rest} other changes weren't applied. | {rest} weitere Änderungen wurden nicht übernommen. | {rest} değişiklik daha uygulanmadı. |  |
| {seconds} seconds | {seconds} Sekunden | {seconds} saniye |  |
| {sentence_case_ref} would make the video {minutes} minutes long and your iPhone builds at most 30, so I left it out | {sentence_case_ref} würde das Video {minutes} Minuten lang machen, und dein iPhone erstellt höchstens 30, deshalb habe ich es weggelassen | {sentence_case_ref} videoyu {minutes} dakikaya çıkarırdı, iPhone'un en fazla 30 dakika hazırlar, o yüzden dışarıda bıraktım |  |
| {sentence_case_words} is clip {v1} of {len_steps}, not the first one. | {sentence_case_words} ist Clip {v1} von {len_steps}, nicht der erste. | {capitalize_words_strip} {len_steps} klipten {v1}. sırada, {v2} değil. |  |
| {sentence_case_words} is clip {v1} of {len_steps}, not the last one. | {sentence_case_words} ist Clip {v1} von {len_steps}, nicht der letzte. | {capitalize_words_strip} {len_steps} klipten {v1}. sırada, {v2} değil. |  |
| {shape} shape needs music; kept a regular montage. | Der Aufbau {shape} braucht Musik; ich habe eine normale Montage gemacht. | {shape} kurgusu için müzik gerekiyor; normal bir montaj yaptım. |  |
| {shown} and {extra} more | {shown} und {extra} weitere | {shown} ve {extra} tane daha |  |
| {some}, so I can't put them in that order. | {some}, deshalb kann ich sie nicht in diese Reihenfolge bringen. | {some}, bu yüzden onları o sıraya koyamıyorum. |  |
| {subject} play at {speed} | {subject} laufen mit {speed} | {subject_tr} {speed} hızda oynuyor |  |
| {subject} play at {speed}, as you asked later | {subject} laufen mit {speed}, wie du es später wolltest | {subject_tr} sonra istediğin gibi {speed} hızda oynuyor |  |
| {subject} play at {speed}, not the {requested:g}x you asked for: a clip plays at least {speed} | {subject} laufen mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft mindestens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en az {speed} hızda oynayabiliyor |  |
| {subject} play at {speed}, not the {requested:g}x you asked for: a clip plays at most {speed} | {subject} laufen mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft höchstens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en fazla {speed} hızda oynayabiliyor |  |
| {subject} play at {speed}, not the {requested:g}x you asked for: the clip ran out of footage | {subject} laufen mit {speed}, nicht mit den gewünschten {requested:g}x: dem Clip ist das Material ausgegangen | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: klibin görüntüsü yetmedi |  |
| {subject} plays at {speed} | {subject} läuft mit {speed} | {subject_tr} {speed} hızda oynuyor |  |
| {subject} plays at {speed}, as you asked later | {subject} läuft mit {speed}, wie du es später wolltest | {subject_tr} sonra istediğin gibi {speed} hızda oynuyor |  |
| {subject} plays at {speed}, not the {requested:g}x you asked for: a clip plays at least {speed} | {subject} läuft mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft mindestens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en az {speed} hızda oynayabiliyor |  |
| {subject} plays at {speed}, not the {requested:g}x you asked for: a clip plays at most {speed} | {subject} läuft mit {speed}, nicht mit den gewünschten {requested:g}x: ein Clip läuft höchstens mit {speed} | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: bir klip en fazla {speed} hızda oynayabiliyor |  |
| {subject} plays at {speed}, not the {requested:g}x you asked for: the clip ran out of footage | {subject} läuft mit {speed}, nicht mit den gewünschten {requested:g}x: dem Clip ist das Material ausgegangen | {subject_tr} {speed} hızda oynuyor, istediğin {requested:g}x değil: klibin görüntüsü yetmedi |  |
| {sum_fails} of {len_rows} don't have the requested {label_0} | {sum_fails} von {len_rows} haben die gewünschte Eigenschaft {label_0} nicht | {len_rows} yazıdan {sum_fails} tanesinde istenen {label_1} yok |  |
| {summary} ({seconds}) | {summary} ({seconds}) | {summary} ({seconds}) |  |
| {summary} I found no clips of {join_unplaced}, so I can't place them where you asked. | {summary} Ich habe keine Clips von {join_unplaced} gefunden, deshalb kann ich sie nicht dort platzieren, wo du es wolltest. | {summary} {join_unplaced} ile ilgili klip bulamadım, bu yüzden onları istediğin yere yerleştiremiyorum. |  |
| {summary}; "{line}" plays whole | {summary}; "{line}" läuft komplett | {summary}; "{line}" baştan sona duyulacak |  |
| {summary}; "{line}" plays whole ({span} of that clip) | {summary}; "{line}" läuft komplett ({span} dieses Clips) | {summary}; "{line}" baştan sona duyuluyor (o klibin {span} kısmı) |  |
| {text} falls outside the picture, in the black bars; I couldn't find a spot on the footage | {text} liegt außerhalb des Bildes, in den schwarzen Balken; ich habe keine Stelle im Material gefunden | {text} görüntünün dışında, siyah bantlara düşüyor; görüntü üzerinde bir yer bulamadım |  |
| {text} is longer than fits on screen, so the video shows “{fact_cut_to}” | {text} ist länger, als auf den Bildschirm passt, deshalb zeigt das Video „{fact_cut_to}“ | {text} ekrana sığmayacak kadar uzun, o yüzden videoda “{fact_cut_to}” görünüyor |  |
| {text} isn't on the video | {text} ist nicht im Video | {text} videoda yok |  |
| {text} lands on a face; I couldn't find a spot clear of it | {text} liegt auf einem Gesicht; ich habe keine Stelle gefunden, an der es frei bleibt | {text} bir yüzün üstüne denk geliyor; yüzü açıkta bırakan bir yer bulamadım |  |
| {text} stays on screen for {held:g} s, not the {asked:g} s you asked for | {text} bleibt {held:g} s auf dem Bildschirm, nicht die gewünschten {asked:g} s | {text} ekranda istediğin {asked:g} sn yerine {held:g} sn kalıyor |  |
| {unnamed} of your recordings never show. | {unnamed} deiner Aufnahmen sind nie zu sehen. | Kayıtlarından {unnamed} tanesi hiç görünmüyor. |  |
| {unnamed} of your recordings never shows. | {unnamed} deiner Aufnahmen ist nie zu sehen. | Kayıtlarından {unnamed} tanesi hiç görünmüyor. |  |
| {v1} and {v2} | {v1} und {v2} | {v1} ve {v2} |  |
| {v1}. | {v1}. | {v2}. |  |
| {v1}; {repeats} more repeated the label before, so I left it off. | {v1}; {repeats} weiterer Clip wiederholte das vorherige Label, deshalb habe ich es dort weggelassen. | {v1}; {repeats} tanesi önceki etiketin aynısıydı, o yüzden koymadım. |  |
| {v1}; {repeats} more repeated the label before, so I left them off. | {v1}; {repeats} weitere Clips wiederholten das vorherige Label, deshalb habe ich es dort weggelassen. | {v1}; {repeats} tanesi önceki etiketin aynısıydı, o yüzden koymadım. |  |
| {v1}; {why}. | {v1}; {why}. | {v2}; {why}. |  |
| {what} is in {count} stretches | {what} liegt in {count} getrennten Abschnitten | {what} {count} parçaya bölünüyor |  |
| {why_capitalize}. | {why_capitalize}. | {why}. |  |
| {words} (clip {n}) | {words} (Clip {n}) | {words} ({n}. klip) |  |
| {words} (first) | {words} (zuerst) | {words} (ilk) |  |
| {words} (last) | {words} (zuletzt) | {words} (son) |  |
| {words} (then) | {words} (danach) | {words} (sonra) |  |
| “{state_candidate}” — does this wording work? | „{state_candidate}“ – passt dir dieser Wortlaut? | “{state_candidate}” — bu ifade sana uyar mı? |  |
| “{title}” | „{title}“ | “{title}” |  |
| “{title}” by {artist} | „{title}“ von {artist} | “{title}” ({artist}) |  |
