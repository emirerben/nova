"""One table for the sentence a failed render shows, in every language (KRI-1382).

`Job.failure_reason` -> a human sentence for the client's failure card (KRI-163).
The English copy used to live in `tasks/content_plan_build.JOB_FAILURE_MESSAGES` and the
Turkish copy in a second table in `tasks/kria_runtime`, kept in step by a test. Both are now
projections of `JOB_FAILURE_REASONS`, so a code cannot exist in one language only, and
`kria/creation_health` reads the same table to label the failure reasons it counts.

A leaf module: it imports nothing, so the task modules and the admin route can all use it.
"""

from __future__ import annotations

from typing import NamedTuple


class JobFailureReason(NamedTuple):
    en: str
    tr: str


JOB_FAILURE_REASONS: dict[str, JobFailureReason] = {
    "creation_text_failed": JobFailureReason(
        en=(
            "I couldn't prepare the text while keeping your approved wording. Your draft "
            "and footage are saved. Try generating this edit again."
        ),
        tr=(
            "Onayladığın yazıyı koruyarak metni hazırlayamadım. Taslağın ve çekimlerin "
            "kayıtlı. Bu videoyu yeniden oluşturmayı dene."
        ),
    ),
    # KRI-466: the creator's song could not be lined up; `error_detail` carries the
    # specific, actionable copy (see CREATOR_FACING_DETAIL_CODES).
    "user_song_plan_declined": JobFailureReason(
        en=(
            "I couldn't line your clips up with your song. Film with the song playing out "
            "loud, or sing along clearly, then try again."
        ),
        tr=(
            "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya "
            "da sözleri net söyleyerek eşlik et, sonra tekrar dene."
        ),
    ),
    # KRI-653: the talking take has no sound; `error_detail` carries the actionable copy.
    "silent_audio_declined": JobFailureReason(
        en=(
            "Your video has no sound, so there's nothing to caption or cut. Record it again "
            "and check that nothing is covering the microphone."
        ),
        tr=(
            "Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey "
            "bulamadım. Tekrar çek ve mikrofonun kapalı olmadığından emin ol."
        ),
    ),
    "phone_plan_unsupported": JobFailureReason(
        en=(
            "This edit uses something your iPhone can't render yet. Start a new edit with a "
            "different clip or format."
        ),
        tr=(
            "Bu düzenleme iPhone'unun henüz oluşturamadığı bir şey içeriyor. Farklı bir "
            "klip ya da formatla yeni bir düzenleme başlat."
        ),
    ),
    # KRI-286: retryable -- the device capability this edit needs is not switched on
    # yet (a rollout decision or an ops kill switch), so the same edit works once it is.
    "phone_capability_unavailable": JobFailureReason(
        en=(
            "This edit needs an iPhone rendering feature that's switched off right now. "
            "Your edit and footage are saved. Try again later, or ask for a different "
            "style."
        ),
        tr=(
            "Bu düzenleme için gereken bir iPhone oluşturma özelliği şu anda kapalı. "
            "Düzenlemen ve çekimlerin kayıtlı. Daha sonra tekrar dene ya da farklı bir stil "
            "iste."
        ),
    ),
    # A provider spend cap or an exhausted AI budget: retrying right away answers the
    # same, so the copy says to wait instead of offering a retry loop.
    "ai_quota_exhausted": JobFailureReason(
        en=(
            "Kria's AI service has reached its usage limit for now, so your video wasn't "
            "made. Your edit and footage are saved. Trying again right away won't help; try "
            "again later."
        ),
        tr=(
            "Kria'nın yapay zekâ hizmeti şimdilik kullanım sınırına ulaştı, bu yüzden "
            "videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Hemen tekrar denemek "
            "işe yaramaz; daha sonra tekrar dene."
        ),
    ),
    # KRI-673: the AI that writes the on-screen text ran out of time on a big edit (prod:
    # 35 clips). Say which step, and what actually helps.
    "creation_text_timeout": JobFailureReason(
        en=(
            "Writing the on-screen text for this many clips took longer than our AI allows, "
            "so your video wasn't made. Your edit and footage are saved. Try again; if it "
            "happens again, use fewer clips or ask for less text."
        ),
        tr=(
            "Bu kadar klip için ekrandaki yazıları hazırlamak yapay zekânın izin verdiği "
            "süreden uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin "
            "kayıtlı. Tekrar dene; yine olursa daha az klip kullan ya da daha az yazı iste."
        ),
    ),
    "plan_ai_timeout": JobFailureReason(
        en=(
            "Our AI took too long to plan this edit, so your video wasn't made. Your edit "
            "and footage are saved. Try again in a moment."
        ),
        tr=(
            "Yapay zekâ bu düzenlemeyi planlarken çok uzun sürdü, bu yüzden videon "
            "oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Birazdan tekrar dene."
        ),
    ),
    # Catch-all for an unclassified planning failure (our bug, or storage that stayed
    # unavailable after the worker's own retries): do not blame the iPhone, it was never
    # involved (KRI-673).
    "phone_plan_failed": JobFailureReason(
        en=(
            "Something broke while planning this edit, so your video wasn't made. Your edit "
            "and footage are saved. Try again, or ask for a change."
        ),
        tr=(
            "Bu düzenleme planlanırken bir şey bozuldu, bu yüzden videon oluşturulmadı. "
            "Düzenlemen ve çekimlerin kayıtlı. Tekrar dene ya da bir değişiklik iste."
        ),
    ),
    # KRI-725: the cloud worker refused a prepared edit before rendering it.
    "prepared_execution_stale": JobFailureReason(
        en=(
            "Something you approved changed before this edit could render, so I didn't make "
            "it. Ask Kria to prepare the edit again."
        ),
        tr=(
            "Onayladığın bir şey bu düzenleme oluşturulmadan önce değişti, bu yüzden "
            "videoyu yapmadım. Kria'dan düzenlemeyi yeniden hazırlamasını iste."
        ),
    ),
    "prepared_execution_unsupported": JobFailureReason(
        en=(
            "This edit was prepared by a newer version of Kria than the one rendering it. "
            "Ask Kria to prepare it again."
        ),
        tr=(
            "Bu düzenleme, onu oluşturan sürümden daha yeni bir Kria sürümüyle hazırlandı. "
            "Kria'dan yeniden hazırlamasını iste."
        ),
    ),
    # A phone project reached the server renderer, which only has preview copies of the
    # clips. Re-attaching uploads previews again, so never tell the creator to do that.
    "originals_not_uploaded": JobFailureReason(
        en=(
            "Only preview copies of your clips are uploaded (the originals stay on your "
            "iPhone), so this edit has to be made on your iPhone. Your edit is saved. Open "
            "the project on the iPhone that has the clips and ask Kria to make it again."
        ),
        tr=(
            "Kliplerinin yalnızca önizleme kopyaları yüklü (orijinaller iPhone'unda "
            "duruyor), bu yüzden bu düzenleme iPhone'unda yapılmalı. Düzenlemen kayıtlı. "
            "Projeyi klipler olan iPhone'da aç ve Kria'dan videoyu yeniden yapmasını iste."
        ),
    ),
    "processing_timeout": JobFailureReason(
        en=(
            "Processing timed out — your clips are heavy (likely 4K/HDR). Try fewer or "
            "shorter clips."
        ),
        tr=(
            "İşlem zaman aşımına uğradı; kliplerin çok ağır (muhtemelen 4K/HDR). Daha az ya "
            "da daha kısa klip dene."
        ),
    ),
    "speech_cleanup_failed": JobFailureReason(
        en="Cleaning up the audio didn't finish. Try again, or turn off speech cleanup.",
        tr="Sesi temizleme tamamlanmadı. Tekrar dene ya da konuşma temizlemeyi kapat.",
    ),
    "no_labeled_tracks": JobFailureReason(
        en="No matching music was found for this edit. Try again, or pick a track yourself.",
        tr="Bu düzenleme için uygun müzik bulunamadı. Tekrar dene ya da müziği kendin seç.",
    ),
    "matching_failed": JobFailureReason(
        en="Matching your footage to music didn't finish. Try again.",
        tr="Çekimlerini müzikle eşleştirme tamamlanmadı. Tekrar dene.",
    ),
    "auto_music_disabled": JobFailureReason(
        en="Automatic music matching is turned off right now. Try again later.",
        tr="Otomatik müzik eşleştirme şu anda kapalı. Daha sonra tekrar dene.",
    ),
    # Server rendering is off and this project was never set up to render on the iPhone
    # (a web or older project), so updating the app changes nothing.
    "cloud_render_disabled": JobFailureReason(
        en=(
            "This project can only be rendered on our servers, and server rendering is "
            "switched off, so this version wasn't made. Start a new edit on your iPhone "
            "with these clips to make it there."
        ),
        tr=(
            "Bu proje yalnızca sunucularımızda oluşturulabiliyor ve sunucuda oluşturma şu "
            "anda kapalı, bu yüzden bu sürüm yapılmadı. Bu kliplerle iPhone'unda yeni bir "
            "düzenleme başlatarak videoyu orada yap."
        ),
    ),
    "dispatch_publish_failed": JobFailureReason(
        en="The render couldn't be handed to the queue. Give it another go.",
        tr="Video sıraya iletilemedi. Bir kez daha dene.",
    ),
    "drive_import_failed": JobFailureReason(
        en="Importing from Drive didn't finish. Try again.",
        tr="Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
    ),
    "drive_import_dispatch_failed": JobFailureReason(
        en="Importing from Drive didn't finish. Try again.",
        tr="Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
    ),
    "upload_promotion_failed": JobFailureReason(
        en="Saving your upload didn't finish. Try again.",
        tr="Yüklemen kaydedilemedi. Tekrar dene.",
    ),
    "skia_disabled": JobFailureReason(
        en="This edit needs a renderer that's temporarily turned off. Try again later.",
        tr=(
            "Bu düzenleme şu anda geçici olarak kapalı bir oluşturucuya ihtiyaç duyuyor. "
            "Daha sonra tekrar dene."
        ),
    ),
    "cancelled_by_admin": JobFailureReason(
        en="This render was cancelled.",
        tr="Bu video iptal edildi.",
    ),
    "render_oom": JobFailureReason(
        en="Rendering ran out of memory — your clips are heavy. Try fewer or shorter clips.",
        tr=(
            "Video hazırlanırken bellek yetmedi; kliplerin çok ağır. Daha az ya da daha "
            "kısa klip dene."
        ),
    ),
    "ffmpeg_failed": JobFailureReason(
        en="Something went wrong while assembling your video. Try again.",
        tr="Videon birleştirilirken bir şeyler ters gitti. Tekrar dene.",
    ),
    "gemini_analysis_failed": JobFailureReason(
        en="Analyzing your footage didn't finish. Try again.",
        tr="Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
    ),
    "analysis_failed": JobFailureReason(
        en="Analyzing your footage didn't finish. Try again.",
        tr="Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
    ),
    "copy_generation_failed": JobFailureReason(
        en="Writing your video's captions didn't finish. Try again.",
        tr="Videonun paylaşım metnini yazma tamamlanmadı. Tekrar dene.",
    ),
    "output_upload_failed": JobFailureReason(
        en="Your finished video couldn't be saved. Try again.",
        tr="Hazır videon kaydedilemedi. Tekrar dene.",
    ),
    "user_clip_download_failed": JobFailureReason(
        en="One of your clips couldn't be downloaded. Reattach it and try again.",
        tr="Kliplerinden biri indirilemedi. Yeniden ekle ve tekrar dene.",
    ),
    "user_clip_unusable": JobFailureReason(
        en=(
            "One of your clips couldn't be used (it may be corrupted or unsupported). Try a "
            "different clip."
        ),
        tr=(
            "Kliplerinden biri kullanılamadı (bozuk ya da desteklenmiyor olabilir). Farklı "
            "bir klip dene."
        ),
    ),
    "template_misconfigured": JobFailureReason(
        en="This template has a configuration problem. Try a different one.",
        tr="Bu şablonda bir yapılandırma sorunu var. Farklı bir tane dene.",
    ),
    "template_assets_missing": JobFailureReason(
        en="This template is missing assets it needs. Try a different one.",
        tr="Bu şablonun ihtiyaç duyduğu dosyalar eksik. Farklı bir tane dene.",
    ),
    "artifact_eligibility_revoked": JobFailureReason(
        en="This export is no longer available.",
        tr="Bu dışa aktarma artık kullanılamıyor.",
    ),
    "eligibility_changed_during_export": JobFailureReason(
        en="Something changed while exporting. Try again.",
        tr="Dışa aktarma sırasında bir şey değişti. Tekrar dene.",
    ),
}

# Never blank: an unrecognized or None-but-status-failed code still needs a real sentence.
DEFAULT_JOB_FAILURE_REASON = JobFailureReason(
    en=(
        "Something went wrong and this render didn't finish. Your direction and "
        "footage are still saved — try again."
    ),
    tr=(
        "Bir şeyler ters gitti ve bu video tamamlanmadı. "
        "Yönün ve çekimlerin hâlâ kayıtlı; tekrar dene."
    ),
)
