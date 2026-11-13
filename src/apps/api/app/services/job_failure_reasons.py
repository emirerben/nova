"""One table for the sentence a failed render shows, in every language (KRI-1382).

`Job.failure_reason` -> a human sentence for the client's failure card (KRI-163).
The English copy used to live in `tasks/content_plan_build.JOB_FAILURE_MESSAGES` and the
Turkish copy in a second table in `tasks/kria_runtime`, kept in step by a test. Both are now
projections of `JOB_FAILURE_REASONS`, so a code cannot exist in one language only, and
`kria/creation_health` reads the same table to label the failure reasons it counts.

Each value is a plain `(english, turkish)` tuple, index 0 and 1, on purpose: the KRI-1362
copy-catalog harvester (`scripts/kria/build_copy_catalog.py`) reads literal tuple tables to
build `kria/copy/tr.json`, and it cannot read a NamedTuple or a comprehension.

A leaf module: it imports nothing, so the task modules and the admin route can all use it.
"""

from __future__ import annotations

JOB_FAILURE_REASONS: dict[str, tuple[str, str]] = {
    "creation_text_failed": (
        "I couldn't prepare the text while keeping your approved wording. Your draft "
        "and footage are saved. Try generating this edit again.",
        "Onayladığın yazıyı koruyarak metni hazırlayamadım. Taslağın ve çekimlerin "
        "kayıtlı. Bu videoyu yeniden oluşturmayı dene.",
    ),
    # KRI-466: the creator's song could not be lined up; `error_detail` carries the
    # specific, actionable copy (see CREATOR_FACING_DETAIL_CODES).
    "user_song_plan_declined": (
        "I couldn't line your clips up with your song. Film with the song playing out "
        "loud, or sing along clearly, then try again.",
        "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya "
        "da sözleri net söyleyerek eşlik et, sonra tekrar dene.",
    ),
    # KRI-653: the talking take has no sound; `error_detail` carries the actionable copy.
    "silent_audio_declined": (
        "Your video has no sound, so there's nothing to caption or cut. Record it again "
        "and check that nothing is covering the microphone.",
        "Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey "
        "bulamadım. Tekrar çek ve mikrofonun kapalı olmadığından emin ol.",
    ),
    "phone_plan_unsupported": (
        "This edit uses something your iPhone can't render yet. Start a new edit with a "
        "different clip or format.",
        "Bu düzenleme iPhone'unun henüz oluşturamadığı bir şey içeriyor. Farklı bir "
        "klip ya da formatla yeni bir düzenleme başlat.",
    ),
    # KRI-286: retryable -- the device capability this edit needs is not switched on
    # yet (a rollout decision or an ops kill switch), so the same edit works once it is.
    "phone_capability_unavailable": (
        "This edit needs an iPhone rendering feature that's switched off right now. "
        "Your edit and footage are saved. Try again later, or ask for a different "
        "style.",
        "Bu düzenleme için gereken bir iPhone oluşturma özelliği şu anda kapalı. "
        "Düzenlemen ve çekimlerin kayıtlı. Daha sonra tekrar dene ya da farklı bir stil "
        "iste.",
    ),
    # A provider spend cap or an exhausted AI budget: retrying right away answers the
    # same, so the copy says to wait instead of offering a retry loop.
    "ai_quota_exhausted": (
        "Kria's AI service has reached its usage limit for now, so your video wasn't "
        "made. Your edit and footage are saved. Trying again right away won't help; try "
        "again later.",
        "Kria'nın yapay zekâ hizmeti şimdilik kullanım sınırına ulaştı, bu yüzden "
        "videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Hemen tekrar denemek "
        "işe yaramaz; daha sonra tekrar dene.",
    ),
    # KRI-673: the AI that writes the on-screen text ran out of time on a big edit (prod:
    # 35 clips). Say which step, and what actually helps.
    "creation_text_timeout": (
        "Writing the on-screen text for this many clips took longer than our AI allows, "
        "so your video wasn't made. Your edit and footage are saved. Try again; if it "
        "happens again, use fewer clips or ask for less text.",
        "Bu kadar klip için ekrandaki yazıları hazırlamak yapay zekânın izin verdiği "
        "süreden uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin "
        "kayıtlı. Tekrar dene; yine olursa daha az klip kullan ya da daha az yazı iste.",
    ),
    "plan_ai_timeout": (
        "Our AI took too long to plan this edit, so your video wasn't made. Your edit "
        "and footage are saved. Try again in a moment.",
        "Yapay zekâ bu düzenlemeyi planlarken çok uzun sürdü, bu yüzden videon "
        "oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Birazdan tekrar dene.",
    ),
    # Catch-all for an unclassified planning failure (our bug, or storage that stayed
    # unavailable after the worker's own retries): do not blame the iPhone, it was never
    # involved (KRI-673).
    "phone_plan_failed": (
        "Something broke while planning this edit, so your video wasn't made. Your edit "
        "and footage are saved. Try again, or ask for a change.",
        "Bu düzenleme planlanırken bir şey bozuldu, bu yüzden videon oluşturulmadı. "
        "Düzenlemen ve çekimlerin kayıtlı. Tekrar dene ya da bir değişiklik iste.",
    ),
    # KRI-725: the cloud worker refused a prepared edit before rendering it.
    "prepared_execution_stale": (
        "Something you approved changed before this edit could render, so I didn't make "
        "it. Ask Kria to prepare the edit again.",
        "Onayladığın bir şey bu düzenleme oluşturulmadan önce değişti, bu yüzden "
        "videoyu yapmadım. Kria'dan düzenlemeyi yeniden hazırlamasını iste.",
    ),
    "prepared_execution_unsupported": (
        "This edit was prepared by a newer version of Kria than the one rendering it. "
        "Ask Kria to prepare it again.",
        "Bu düzenleme, onu oluşturan sürümden daha yeni bir Kria sürümüyle hazırlandı. "
        "Kria'dan yeniden hazırlamasını iste.",
    ),
    # A phone project reached the server renderer, which only has preview copies of the
    # clips. Re-attaching uploads previews again, so never tell the creator to do that.
    "originals_not_uploaded": (
        "Only preview copies of your clips are uploaded (the originals stay on your "
        "iPhone), so this edit has to be made on your iPhone. Your edit is saved. Open "
        "the project on the iPhone that has the clips and ask Kria to make it again.",
        "Kliplerinin yalnızca önizleme kopyaları yüklü (orijinaller iPhone'unda "
        "duruyor), bu yüzden bu düzenleme iPhone'unda yapılmalı. Düzenlemen kayıtlı. "
        "Projeyi klipler olan iPhone'da aç ve Kria'dan videoyu yeniden yapmasını iste.",
    ),
    "processing_timeout": (
        "Processing timed out — your clips are heavy (likely 4K/HDR). Try fewer or shorter clips.",
        "İşlem zaman aşımına uğradı; kliplerin çok ağır (muhtemelen 4K/HDR). Daha az ya "
        "da daha kısa klip dene.",
    ),
    "speech_cleanup_failed": (
        "Cleaning up the audio didn't finish. Try again, or turn off speech cleanup.",
        "Sesi temizleme tamamlanmadı. Tekrar dene ya da konuşma temizlemeyi kapat.",
    ),
    "no_labeled_tracks": (
        "No matching music was found for this edit. Try again, or pick a track yourself.",
        "Bu düzenleme için uygun müzik bulunamadı. Tekrar dene ya da müziği kendin seç.",
    ),
    "matching_failed": (
        "Matching your footage to music didn't finish. Try again.",
        "Çekimlerini müzikle eşleştirme tamamlanmadı. Tekrar dene.",
    ),
    "auto_music_disabled": (
        "Automatic music matching is turned off right now. Try again later.",
        "Otomatik müzik eşleştirme şu anda kapalı. Daha sonra tekrar dene.",
    ),
    # Server rendering is off and this project was never set up to render on the iPhone
    # (a web or older project), so updating the app changes nothing.
    "cloud_render_disabled": (
        "This project can only be rendered on our servers, and server rendering is "
        "switched off, so this version wasn't made. Start a new edit on your iPhone "
        "with these clips to make it there.",
        "Bu proje yalnızca sunucularımızda oluşturulabiliyor ve sunucuda oluşturma şu "
        "anda kapalı, bu yüzden bu sürüm yapılmadı. Bu kliplerle iPhone'unda yeni bir "
        "düzenleme başlatarak videoyu orada yap.",
    ),
    "dispatch_publish_failed": (
        "The render couldn't be handed to the queue. Give it another go.",
        "Video sıraya iletilemedi. Bir kez daha dene.",
    ),
    "drive_import_failed": (
        "Importing from Drive didn't finish. Try again.",
        "Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
    ),
    "drive_import_dispatch_failed": (
        "Importing from Drive didn't finish. Try again.",
        "Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
    ),
    "upload_promotion_failed": (
        "Saving your upload didn't finish. Try again.",
        "Yüklemen kaydedilemedi. Tekrar dene.",
    ),
    "skia_disabled": (
        "This edit needs a renderer that's temporarily turned off. Try again later.",
        "Bu düzenleme şu anda geçici olarak kapalı bir oluşturucuya ihtiyaç duyuyor. "
        "Daha sonra tekrar dene.",
    ),
    "cancelled_by_admin": ("This render was cancelled.", "Bu video iptal edildi."),
    "render_oom": (
        "Rendering ran out of memory — your clips are heavy. Try fewer or shorter clips.",
        "Video hazırlanırken bellek yetmedi; kliplerin çok ağır. Daha az ya da daha "
        "kısa klip dene.",
    ),
    "ffmpeg_failed": (
        "Something went wrong while assembling your video. Try again.",
        "Videon birleştirilirken bir şeyler ters gitti. Tekrar dene.",
    ),
    "gemini_analysis_failed": (
        "Analyzing your footage didn't finish. Try again.",
        "Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
    ),
    "analysis_failed": (
        "Analyzing your footage didn't finish. Try again.",
        "Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
    ),
    "copy_generation_failed": (
        "Writing your video's captions didn't finish. Try again.",
        "Videonun paylaşım metnini yazma tamamlanmadı. Tekrar dene.",
    ),
    "output_upload_failed": (
        "Your finished video couldn't be saved. Try again.",
        "Hazır videon kaydedilemedi. Tekrar dene.",
    ),
    "user_clip_download_failed": (
        "One of your clips couldn't be downloaded. Reattach it and try again.",
        "Kliplerinden biri indirilemedi. Yeniden ekle ve tekrar dene.",
    ),
    "user_clip_unusable": (
        "One of your clips couldn't be used (it may be corrupted or unsupported). Try a "
        "different clip.",
        "Kliplerinden biri kullanılamadı (bozuk ya da desteklenmiyor olabilir). Farklı "
        "bir klip dene.",
    ),
    "template_misconfigured": (
        "This template has a configuration problem. Try a different one.",
        "Bu şablonda bir yapılandırma sorunu var. Farklı bir tane dene.",
    ),
    "template_assets_missing": (
        "This template is missing assets it needs. Try a different one.",
        "Bu şablonun ihtiyaç duyduğu dosyalar eksik. Farklı bir tane dene.",
    ),
    "artifact_eligibility_revoked": (
        "This export is no longer available.",
        "Bu dışa aktarma artık kullanılamıyor.",
    ),
    "eligibility_changed_during_export": (
        "Something changed while exporting. Try again.",
        "Dışa aktarma sırasında bir şey değişti. Tekrar dene.",
    ),
}

# Never blank: an unrecognized or None-but-status-failed code still needs a real sentence.
DEFAULT_JOB_FAILURE_REASON: tuple[str, str] = (
    "Something went wrong and this render didn't finish. Your direction and "
    "footage are still saved — try again.",
    "Bir şeyler ters gitti ve bu video tamamlanmadı. "
    "Yönün ve çekimlerin hâlâ kayıtlı; tekrar dene.",
)
