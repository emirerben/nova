"use client";

import Link from "next/link";
import {
  CSSProperties,
  MutableRefObject,
  RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import styles from "./KriaEditStory.module.css";
import {
  AUTO_RENDER_START_MS,
  AUTO_STORY_DURATION_MS,
  getAutoKriaHeadlineLineCount,
  getAutoKriaStoryStep,
  getAutoStoryAudioVolume,
  getKriaHeadlineLineCount,
  getKriaStoryStep,
  KRIA_STORY_STEPS,
} from "./KriaEditStorySteps";

const RAW_MEDIA = {
  landscape: "/landing/raw-story/lisbon.mp4",
  portraitLeft: "/landing/raw-story/istanbul.mp4",
  portraitRight: "/landing/raw-story/alberobello.mp4",
  imageOverlay: "/landing/raw-story/trulli-street.jpg",
  videoOverlay: "/landing/raw-story/corfu.mp4",
  rendered: "/landing/raw-story/travel-render.mp4",
} as const;

const RAW_POSTERS = {
  landscape: "/landing/raw-story/lisbon.jpg",
  portraitLeft: "/landing/raw-story/istanbul.jpg",
  portraitRight: "/landing/raw-story/alberobello.jpg",
  videoOverlay: "/landing/raw-story/corfu.jpg",
  rendered: "/landing/raw-story/travel-render.jpg",
} as const;

const RENDERED_AUDIO = "/landing/raw-story/travel-reference-audio.m4a";
const RENDERED_DRIFT_CORRECTION_INTERVAL_MS = 500;
const RENDERED_VIDEO_INDEX = 3;
const SCROLL_RENDER_SEEK_BY_STEP: Readonly<Record<number, number>> = {
  4: 0.5,
  5: 1.5,
  6: 2,
  7: 7.1,
};

const ACCESSIBLE_STORY_STEPS = [
  "The phone starts empty while your raw footage and editing tools wait around it.",
  "Kria adds the first landscape clip and begins the edit.",
  "Kria adds the next two clips to build a multi-shot sequence.",
  "Captions and visual effects arrive together and appear on the video.",
  "The overlay inputs land, and the rendered video reveals the finished placement.",
  "Sound effects arrive, and only then does the finished edit begin playing music.",
  "The finished message appears one centered line at a time: Save time, let AI edit your videos, and create more.",
] as const;

type TravelKey =
  | "rawOne"
  | "rawTwo"
  | "rawThree"
  | "captions"
  | "placeOverlay"
  | "imageOverlay"
  | "videoOverlay"
  | "visualEffects"
  | "sound";

const TRAVEL_KEYS: TravelKey[] = [
  "rawOne",
  "rawTwo",
  "rawThree",
  "captions",
  "placeOverlay",
  "imageOverlay",
  "videoOverlay",
  "visualEffects",
  "sound",
];

function createTravelRefs(): Record<TravelKey, MutableRefObject<HTMLElement | null>> {
  return Object.fromEntries(
    TRAVEL_KEYS.map((key) => [key, { current: null }]),
  ) as Record<TravelKey, MutableRefObject<HTMLElement | null>>;
}

type TravelStyles = Partial<Record<TravelKey, CSSProperties>>;

export type KriaEditStoryMode = "scroll" | "auto";

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function PosterImage({ poster, className }: { poster: string; className?: string }) {
  return (
    <div
      className={`${styles.posterImage} ${className ?? ""}`}
      style={{ backgroundImage: `url(${poster})` }}
      aria-hidden="true"
    />
  );
}

function StoryVideo({
  src,
  poster,
  className,
  videoRef,
  loop = true,
  onCanPlay,
}: {
  src: string;
  poster: string;
  className?: string;
  videoRef: (node: HTMLVideoElement | null) => void;
  loop?: boolean;
  onCanPlay?: () => void;
}) {
  return (
    <video
      ref={videoRef}
      className={className}
      src={src}
      poster={poster}
      muted
      loop={loop}
      playsInline
      preload="metadata"
      onCanPlay={onCanPlay}
      aria-hidden="true"
    />
  );
}

function useTravelStyles(
  sources: Record<TravelKey, RefObject<HTMLElement>>,
  targets: Record<TravelKey, RefObject<HTMLElement>>,
) {
  const [travelStyles, setTravelStyles] = useState<TravelStyles>({});

  useEffect(() => {
    let raf = 0;

    const measure = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const next: TravelStyles = {};
        (Object.keys(sources) as TravelKey[]).forEach((key) => {
          const source = sources[key].current;
          const target = targets[key].current;
          if (!source || !target) return;

          const sourceParent = source.offsetParent;
          const sourceParentRect = sourceParent?.getBoundingClientRect();
          const transformedSourceRect = source.getBoundingClientRect();
          const from = sourceParentRect
            ? {
                left: sourceParentRect.left + source.offsetLeft,
                top: sourceParentRect.top + source.offsetTop,
                width: source.offsetWidth || transformedSourceRect.width,
                height: source.offsetHeight || transformedSourceRect.height,
              }
            : transformedSourceRect;
          const to = target.getBoundingClientRect();
          const scale = clamp(
            Math.min(to.width / Math.max(from.width, 1), to.height / Math.max(from.height, 1)),
            0.24,
            0.72,
          );

          next[key] = {
            "--travel-x": `${to.left + to.width / 2 - (from.left + from.width / 2)}px`,
            "--travel-y": `${to.top + to.height / 2 - (from.top + from.height / 2)}px`,
            "--travel-scale": scale,
          } as CSSProperties;
        });
        setTravelStyles(next);
      });
    };

    measure();
    window.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", measure);
    };
  }, [sources, targets]);

  return travelStyles;
}

export default function KriaEditStory({ mode = "scroll" }: { mode?: KriaEditStoryMode }) {
  const isAuto = mode === "auto";
  const sectionRef = useRef<HTMLElement>(null);
  const [progress, setProgress] = useState(0);
  const progressRef = useRef(0);
  const [step, setStep] = useState(0);
  const [headlineLines, setHeadlineLines] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [autoPlaying, setAutoPlaying] = useState(false);
  const [soundUnavailable, setSoundUnavailable] = useState(false);
  const phoneVideos = useRef<Array<HTMLVideoElement | null>>([]);
  const overlayVideo = useRef<HTMLVideoElement | null>(null);
  const ambienceAudio = useRef<HTMLAudioElement | null>(null);
  const autoStartedAt = useRef(0);
  const renderedPlaybackAttempted = useRef(false);
  const renderedPlaybackRetryPending = useRef(false);
  const renderedPlaybackRetryUsed = useRef(false);
  const renderedPlaybackGeneration = useRef(0);
  const audioPlaybackGeneration = useRef(0);
  const lastRenderedCorrectionAt = useRef(Number.NEGATIVE_INFINITY);
  const reducedMotionAudioTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const sourceRefs = useMemo(
    createTravelRefs,
    [],
  );
  const targetRefs = useMemo(
    createTravelRefs,
    [],
  );
  const travelStyles = useTravelStyles(sourceRefs, targetRefs);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => {
      setReducedMotion(query.matches);
      if (query.matches) {
        progressRef.current = 1;
        setProgress(1);
        setStep(7);
        setHeadlineLines(3);
      }
    };

    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    const sourceVideo = overlayVideo.current;
    const playbackBlocked = reducedMotion || (isAuto && !autoPlaying);

    if (sourceVideo) {
      if (playbackBlocked || step !== 4) {
        sourceVideo.pause();
      } else {
        const playback = sourceVideo.play();
        if (playback) void playback.catch(() => undefined);
      }
    }
  }, [autoPlaying, isAuto, reducedMotion, step]);

  useEffect(() => {
    if (!isAuto || !reducedMotion) return;
    ambienceAudio.current?.pause();
    phoneVideos.current.forEach((video) => video?.pause());
    if (reducedMotionAudioTimer.current) {
      clearTimeout(reducedMotionAudioTimer.current);
      reducedMotionAudioTimer.current = null;
    }
    renderedPlaybackAttempted.current = false;
    renderedPlaybackRetryPending.current = false;
    renderedPlaybackRetryUsed.current = false;
    renderedPlaybackGeneration.current += 1;
    audioPlaybackGeneration.current += 1;
    setSoundUnavailable(false);
    lastRenderedCorrectionAt.current = Number.NEGATIVE_INFINITY;
    setAutoPlaying(false);
  }, [isAuto, reducedMotion]);

  useEffect(() => {
    if (isAuto || reducedMotion || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let raf = 0;
    let lastProgress = -1;
    const update = () => {
      raf = 0;
      const section = sectionRef.current;
      if (!section) return;

      const distance = Math.max(section.offsetHeight - window.innerHeight, 1);
      const nextProgress = clamp((window.scrollY - section.offsetTop) / distance, 0, 1);
      if (Math.abs(nextProgress - lastProgress) < 0.001 && nextProgress !== 0 && nextProgress !== 1) return;

      lastProgress = nextProgress;
      setStep(getKriaStoryStep(nextProgress));
      setHeadlineLines(getKriaHeadlineLineCount(nextProgress));
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };

    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [isAuto, reducedMotion]);

  useEffect(() => {
    if (!isAuto || !autoPlaying || reducedMotion) return;

    let raf = 0;
    const tick = (now: number) => {
      const audio = ambienceAudio.current;
      const elapsed = audio && !audio.paused ? audio.currentTime * 1_000 : now - autoStartedAt.current;
      const nextProgress = clamp(elapsed / AUTO_STORY_DURATION_MS, 0, 1);
      const nextStep = getAutoKriaStoryStep(elapsed);
      const renderedVideo = phoneVideos.current[RENDERED_VIDEO_INDEX];

      const previousProgress = progressRef.current;
      progressRef.current = nextProgress;
      if (previousProgress === 0 && nextProgress > 0) setProgress(nextProgress);
      if (previousProgress < 1 && nextProgress >= 1) setProgress(1);
      setStep(nextStep);
      setHeadlineLines(getAutoKriaHeadlineLineCount(elapsed));
      if (audio) audio.volume = getAutoStoryAudioVolume(elapsed);

      if (renderedVideo && elapsed >= AUTO_RENDER_START_MS) {
        const referenceTime = (elapsed - AUTO_RENDER_START_MS) / 1_000;
        if (!renderedPlaybackAttempted.current) {
          renderedPlaybackAttempted.current = true;
          renderedVideo.currentTime = referenceTime;
          const playbackGeneration = renderedPlaybackGeneration.current;
          const playback = renderedVideo.play();
          if (playback) {
            void playback.catch(() => {
              if (renderedPlaybackGeneration.current === playbackGeneration) {
                renderedPlaybackRetryPending.current = true;
              }
            });
          }
        } else if (
          !renderedVideo.paused
          && !renderedVideo.seeking
          && renderedVideo.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA
          && elapsed - lastRenderedCorrectionAt.current >= RENDERED_DRIFT_CORRECTION_INTERVAL_MS
          && Math.abs(renderedVideo.currentTime - referenceTime) > 0.08
        ) {
          renderedVideo.currentTime = referenceTime;
          lastRenderedCorrectionAt.current = elapsed;
        }
      }

      if (nextProgress >= 1) {
        setAutoPlaying(false);
        audio?.pause();
        renderedVideo?.pause();
        audioPlaybackGeneration.current += 1;
        renderedPlaybackGeneration.current += 1;
        renderedPlaybackRetryPending.current = false;
        return;
      }
      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [autoPlaying, isAuto, reducedMotion]);

  useEffect(() => {
    const audio = ambienceAudio.current;
    return () => {
      audioPlaybackGeneration.current += 1;
      renderedPlaybackGeneration.current += 1;
      audio?.pause();
      if (reducedMotionAudioTimer.current) clearTimeout(reducedMotionAudioTimer.current);
    };
  }, []);

  useEffect(() => {
    const activeIndex = step === 0 ? -1 : step <= 3 ? step - 1 : RENDERED_VIDEO_INDEX;
    phoneVideos.current.forEach((video, index) => {
      if (!video) return;
      if (index !== activeIndex) {
        video.pause();
        return;
      }
      if (reducedMotion || (isAuto && !autoPlaying)) return;

      if (isAuto && index === RENDERED_VIDEO_INDEX) return;

      video.currentTime =
        index === RENDERED_VIDEO_INDEX ? SCROLL_RENDER_SEEK_BY_STEP[step] ?? 0 : 0;
      const playback = video.play();
      if (playback) void playback.catch(() => undefined);
    });
  }, [autoPlaying, isAuto, reducedMotion, step]);

  const toggleAutomaticStory = () => {
    if (!isAuto) return;

    if (autoPlaying) {
      audioPlaybackGeneration.current += 1;
      renderedPlaybackGeneration.current += 1;
      ambienceAudio.current?.pause();
      phoneVideos.current.forEach((video) => video?.pause());
      if (reducedMotionAudioTimer.current) {
        clearTimeout(reducedMotionAudioTimer.current);
        reducedMotionAudioTimer.current = null;
      }
      setAutoPlaying(false);
      setSoundUnavailable(false);
      renderedPlaybackAttempted.current = false;
      renderedPlaybackRetryPending.current = false;
      renderedPlaybackRetryUsed.current = false;
      lastRenderedCorrectionAt.current = Number.NEGATIVE_INFINITY;
      return;
    }

    const currentProgress = progressRef.current;
    const isResuming = currentProgress > 0 && currentProgress < 1;
    const resumeElapsedMs = isResuming ? currentProgress * AUTO_STORY_DURATION_MS : 0;

    const audio = ambienceAudio.current;
    if (audio) {
      audio.currentTime = resumeElapsedMs / 1_000;
      audio.volume = reducedMotion ? 0.8 : getAutoStoryAudioVolume(audio.currentTime * 1_000);
      audio.loop = false;
      const playbackGeneration = audioPlaybackGeneration.current + 1;
      audioPlaybackGeneration.current = playbackGeneration;
      setSoundUnavailable(false);
      const playback = audio.play();
      if (playback) {
        void playback.catch(() => {
          if (audioPlaybackGeneration.current === playbackGeneration) {
            setSoundUnavailable(true);
          }
        });
      }
    }

    if (reducedMotion) {
      progressRef.current = 1;
      setProgress(1);
      setStep(7);
      setHeadlineLines(3);
      setAutoPlaying(true);
      if (audio) audio.currentTime = AUTO_RENDER_START_MS / 1_000;
      const renderedVideo = phoneVideos.current[RENDERED_VIDEO_INDEX];
      if (renderedVideo) {
        renderedPlaybackAttempted.current = true;
        renderedPlaybackRetryPending.current = false;
        renderedPlaybackRetryUsed.current = false;
        renderedPlaybackGeneration.current += 1;
        renderedVideo.currentTime = 0;
        const playbackGeneration = renderedPlaybackGeneration.current;
        const playback = renderedVideo.play();
        if (playback) {
          void playback.catch(() => {
            if (renderedPlaybackGeneration.current === playbackGeneration) {
              renderedPlaybackRetryPending.current = true;
            }
          });
        }
      }
      if (reducedMotionAudioTimer.current) clearTimeout(reducedMotionAudioTimer.current);
      reducedMotionAudioTimer.current = setTimeout(() => {
        audioPlaybackGeneration.current += 1;
        renderedPlaybackGeneration.current += 1;
        ambienceAudio.current?.pause();
        phoneVideos.current[RENDERED_VIDEO_INDEX]?.pause();
        renderedPlaybackRetryPending.current = false;
        setAutoPlaying(false);
        reducedMotionAudioTimer.current = null;
      }, AUTO_STORY_DURATION_MS - AUTO_RENDER_START_MS);
      return;
    }

    if (!isResuming) {
      progressRef.current = 0;
      setProgress(0);
      setStep(0);
      setHeadlineLines(0);
      const renderedVideo = phoneVideos.current[RENDERED_VIDEO_INDEX];
      if (renderedVideo) renderedVideo.currentTime = 0;
      if (overlayVideo.current) overlayVideo.current.currentTime = 0;
    }
    const elapsed = audio
      ? audio.currentTime * 1_000
      : resumeElapsedMs;
    autoStartedAt.current = performance.now() - elapsed;
    renderedPlaybackAttempted.current = false;
    renderedPlaybackRetryPending.current = false;
    renderedPlaybackRetryUsed.current = false;
    renderedPlaybackGeneration.current += 1;
    lastRenderedCorrectionAt.current = Number.NEGATIVE_INFINITY;
    setAutoPlaying(true);
  };

  const phoneVideoRefs = useMemo(
    () => [0, 1, 2, RENDERED_VIDEO_INDEX].map((index) => (node: HTMLVideoElement | null) => {
      phoneVideos.current[index] = node;
    }),
    [],
  );

  const autoButtonLabel = autoPlaying
    ? soundUnavailable
      ? "Pause — playing without sound"
      : "Pause automatic demo"
    : progress >= 1
      ? "Replay with sound"
      : progress > 0
        ? "Resume with sound"
        : "Play with sound";

  return (
    <section
      ref={sectionRef}
      className={styles.scrollStory}
      data-mode={mode}
      data-reduced-motion={reducedMotion}
      aria-label="How Kria turns raw videos into a finished edit"
    >
      {isAuto ? (
        <audio
          ref={ambienceAudio}
          src={RENDERED_AUDIO}
          preload="metadata"
          data-testid="auto-story-audio"
          aria-hidden="true"
        />
      ) : null}
      <div className={styles.srOnly}>
        <p>How Kria builds your edit:</p>
        <ol>
          {ACCESSIBLE_STORY_STEPS.map((storyStep) => (
            <li key={storyStep}>{storyStep}</li>
          ))}
        </ol>
      </div>
      <div className={styles.stickyStage} data-step={step}>
        <p className={styles.srOnly} aria-live="polite">
          {KRIA_STORY_STEPS[step].label}, {KRIA_STORY_STEPS[step].range}
        </p>

        <div className={styles.storyControls}>
          <nav className={styles.modeSwitch} aria-label="Compare animation versions">
            <Link href="/?mode=scroll" aria-current={!isAuto ? "page" : undefined}>Scroll</Link>
            <Link href="/" aria-current={isAuto ? "page" : undefined}>Automatic</Link>
          </nav>
          {isAuto ? (
            <button
              type="button"
              className={styles.autoPlayButton}
              onClick={toggleAutomaticStory}
              aria-pressed={autoPlaying}
            >
              <span aria-hidden="true">{autoPlaying ? "Ⅱ" : "▶"}</span> {autoButtonLabel}
            </button>
          ) : null}
        </div>

        <h1
          className={styles.headline}
          data-image-blend="difference"
          data-screen-alignment="center"
          aria-label="Save time. Let AI edit your videos. Create more."
        >
          <span className={styles.headlineOne} data-active={headlineLines === 1}>Save time</span>
          <span className={styles.headlineTwo} data-active={headlineLines === 2}>Let AI edit your videos</span>
          <span className={styles.headlineThree} data-active={headlineLines === 3}>Create more</span>
        </h1>

        <div
          ref={(node) => { sourceRefs.rawOne.current = node; }}
          className={`${styles.sourceCard} ${styles.rawOne}`}
          data-consumed={step >= 1}
          style={travelStyles.rawOne}
          aria-hidden="true"
        >
          <PosterImage poster={RAW_POSTERS.landscape} className={styles.sourceMedia} />
        </div>
        <div
          ref={(node) => { sourceRefs.rawTwo.current = node; }}
          className={`${styles.sourceCard} ${styles.rawTwo}`}
          data-consumed={step >= 2}
          style={travelStyles.rawTwo}
          aria-hidden="true"
        >
          <PosterImage poster={RAW_POSTERS.portraitLeft} className={styles.sourceMedia} />
        </div>
        <div
          ref={(node) => { sourceRefs.rawThree.current = node; }}
          className={`${styles.sourceCard} ${styles.rawThree}`}
          data-consumed={step >= 3}
          style={travelStyles.rawThree}
          aria-hidden="true"
        >
          <PosterImage poster={RAW_POSTERS.portraitRight} className={styles.sourceMedia} />
        </div>

        <div
          ref={(node) => { sourceRefs.imageOverlay.current = node; }}
          className={`${styles.sourceCard} ${styles.imageSource} ${styles.effectSource}`}
          data-consumed={step >= 5}
          style={travelStyles.imageOverlay}
          aria-hidden="true"
        >
          <PosterImage poster={RAW_MEDIA.imageOverlay} className={styles.sourceMedia} />
        </div>
        <div
          ref={(node) => { sourceRefs.videoOverlay.current = node; }}
          className={`${styles.sourceCard} ${styles.videoSource} ${styles.effectSource}`}
          data-consumed={step >= 5}
          style={travelStyles.videoOverlay}
          aria-hidden="true"
        >
          <StoryVideo
            src={RAW_MEDIA.videoOverlay}
            poster={RAW_POSTERS.videoOverlay}
            videoRef={(node) => { overlayVideo.current = node; }}
            className={styles.sourceMedia}
          />
        </div>

        <div
          ref={(node) => { sourceRefs.captions.current = node; }}
          className={`${styles.featureChip} ${styles.captionChip}`}
          data-consumed={step >= 4}
          data-feature-group="captions-effects"
          style={travelStyles.captions}
          aria-hidden="true"
        >
          Captions
        </div>
        <div
          ref={(node) => { sourceRefs.placeOverlay.current = node; }}
          className={`${styles.featureChip} ${styles.overlayChip}`}
          data-consumed={step >= 5}
          style={travelStyles.placeOverlay}
          aria-hidden="true"
        >
          Place overlays
        </div>
        <div
          ref={(node) => { sourceRefs.visualEffects.current = node; }}
          className={`${styles.featureChip} ${styles.visualEffectsChip}`}
          data-consumed={step >= 4}
          data-feature-group="captions-effects"
          style={travelStyles.visualEffects}
          aria-hidden="true"
        >
          Add visual effects
        </div>
        <div
          ref={(node) => { sourceRefs.sound.current = node; }}
          className={`${styles.featureChip} ${styles.soundChip}`}
          data-consumed={step >= 7}
          style={travelStyles.sound}
          aria-hidden="true"
        >
          Add sound effects
        </div>

        <div className={styles.phone} aria-hidden="true">
          <div className={styles.phoneScreen}>
            <span ref={targetRefs.rawOne} className={styles.rawOneTarget} />
            <span ref={targetRefs.rawTwo} className={styles.rawTwoTarget} />
            <span ref={targetRefs.rawThree} className={styles.rawThreeTarget} />
            <span ref={targetRefs.captions} className={styles.captionsTarget} />
            <span ref={targetRefs.placeOverlay} className={styles.overlayTarget} />
            <span ref={targetRefs.imageOverlay} className={styles.imageOverlayTarget} />
            <span ref={targetRefs.videoOverlay} className={styles.videoOverlayTarget} />
            <span ref={targetRefs.visualEffects} className={styles.visualEffectsTarget} />
            <span ref={targetRefs.sound} className={styles.soundTarget} />

            <StoryVideo
              src={RAW_MEDIA.landscape}
              poster={RAW_POSTERS.landscape}
              videoRef={phoneVideoRefs[0]}
              className={`${styles.phoneShot} ${step === 1 ? styles.activeShot : ""}`}
            />
            <StoryVideo
              src={RAW_MEDIA.portraitLeft}
              poster={RAW_POSTERS.portraitLeft}
              videoRef={phoneVideoRefs[1]}
              className={`${styles.phoneShot} ${step === 2 ? styles.activeShot : ""}`}
            />
            <StoryVideo
              src={RAW_MEDIA.portraitRight}
              poster={RAW_POSTERS.portraitRight}
              videoRef={phoneVideoRefs[2]}
              className={`${styles.phoneShot} ${step === 3 ? styles.activeShot : ""}`}
            />
            <StoryVideo
              src={RAW_MEDIA.rendered}
              poster={RAW_POSTERS.rendered}
              videoRef={phoneVideoRefs[3]}
              onCanPlay={() => {
                const video = phoneVideos.current[RENDERED_VIDEO_INDEX];
                if (
                  !video
                  || !isAuto
                  || !autoPlaying
                  || !renderedPlaybackRetryPending.current
                  || renderedPlaybackRetryUsed.current
                ) return;
                renderedPlaybackRetryPending.current = false;
                renderedPlaybackRetryUsed.current = true;
                const playback = video.play();
                if (playback) void playback.catch(() => undefined);
              }}
              loop={false}
              className={`${styles.phoneShot} ${step >= 4 ? styles.activeShot : ""}`}
            />
          </div>
        </div>

        <Link href="/plan" className={styles.cta}>
          Create my first edit <span aria-hidden="true">→</span>
        </Link>
      </div>
    </section>
  );
}
