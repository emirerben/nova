"use client";

import Link from "next/link";
import { Pause, Play } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import KriaMark from "@/components/KriaMark";
import { Button } from "@/components/ui/button";
import { motionCubicBezier } from "@/lib/overlay-animation";

import styles from "./KriaLifeLanding.module.css";

const HERO_VIDEO = "/landing/life/hero-loop-v5.mp4";
const HERO_POSTER = "/landing/life/hero-poster-v5.jpg";
const KRIA_WORDMARK = "/landing/life/kria-wordmark-white-outlined.svg";
const PRODUCT_DEMO_VIDEO = "/landing/product-demo/kria-product-flow.mp4";
const PRODUCT_DEMO_POSTER = "/landing/product-demo/kria-product-flow-poster.jpg";
const SHOW_FOLLOWUP_SECTIONS = false;
const PAYOFF_FALLBACK_MS = 3200;

const PRODUCT_DEMO_STEPS = [
  {
    title: "Tell Kria what you want to make",
    body: "Ask for an edit in your own words, then add the clips you want Kria to work with. The brief and footage stay together in one conversation.",
  },
  {
    title: "Start from a real first cut",
    body: "Kria reads the footage, finds the opening, shapes the rhythm, and renders a complete edit while you keep working or step away.",
  },
  {
    title: "See the whole edit take shape",
    body: "Play the result in the full editor, then trim clips, rewrite text, change the sound, or tune the visual style whenever you want hands-on control.",
  },
  {
    title: "Keep directing in chat",
    body: "Ask Kria to sharpen the opening, tighten the text, or try another direction. Every note becomes the next version without starting over.",
  },
] as const;

const PRODUCTION_WORDMARK_LETTERS = [
  {
    glyph: "k",
    definition: "font_3_205",
    transform: "matrix(42.192846,-5.929818,-5.929818,-42.192846,7.3646118,78.39235)",
    className: "wordmarkK",
  },
  {
    glyph: "r",
    definition: "font_4_221",
    transform: "matrix(40.820325,2.8544348,2.8544348,-40.820325,35.041086,83.16414)",
    className: "wordmarkR",
  },
  {
    glyph: "i",
    definition: "font_4_196",
    transform: "matrix(40.764287,-3.566413,-3.566413,-40.764287,59.32224,78.61586)",
    className: "wordmarkI",
  },
  {
    glyph: "a",
    definition: "font_3_175",
    transform: "matrix(42.289915,5.1925485,5.1925485,-42.289915,72.0213,79.226078)",
    className: "wordmarkA",
  },
] as const;

type LandingHeroStage = "fullscreen" | "wordmark" | "complete";

function getLandingHeroStage(currentTime: number): LandingHeroStage {
  if (currentTime >= 2.9) return "complete";
  if (currentTime >= 0.8) return "wordmark";
  return "fullscreen";
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function rangeProgress(value: number, start: number, end: number): number {
  return clamp((value - start) / Math.max(end - start, 0.001));
}

export function getProductDemoStep(currentTime: number): number {
  if (currentTime >= 9.8) return 3;
  if (currentTime >= 5.8) return 2;
  if (currentTime >= 2.2) return 1;
  return 0;
}

export function getLandingScrollProgress(
  scrollY: number,
  sectionTop: number,
  sectionHeight: number,
  viewportHeight: number,
): number {
  const distance = Math.max(sectionHeight - viewportHeight, 1);
  return clamp((scrollY - sectionTop) / distance);
}

export function getLandingMorphState(progress: number) {
  const eased = motionCubicBezier(clamp(progress), 0.76, 0, 0.24, 1);
  const collapse = rangeProgress(eased, 0, 0.94);
  const logoReveal = rangeProgress(eased, 0.12, 0.92);

  return {
    fullOpacity: 1 - rangeProgress(eased, 0.54, 0.96),
    fullScale: 1 - collapse * 0.06,
    insetX: collapse * 26,
    insetY: collapse * 35,
    radius: collapse * 54,
    // The logo starts as the pure-white production mark. Scrolling then
    // crossfades to the synchronized reel masked by those exact glyphs.
    logoOpacity: logoReveal,
    logoScale: 1.08 - logoReveal * 0.08,
    wordmarkOpacity: 1 - logoReveal,
    chromeOpacity: 1 - rangeProgress(eased, 0, 0.24),
  };
}

export default function KriaLifeLanding() {
  const scrollSectionRef = useRef<HTMLElement>(null);
  const heroRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const logoVideoRef = useRef<HTMLVideoElement>(null);
  const fullVideoLayerRef = useRef<HTMLDivElement>(null);
  const logoVideoLayerRef = useRef<SVGSVGElement>(null);
  const logoMaskRef = useRef<SVGMaskElement>(null);
  const logoMaskImageRef = useRef<SVGImageElement>(null);
  const logoForeignObjectRef = useRef<SVGForeignObjectElement>(null);
  const wordmarkRef = useRef<SVGSVGElement>(null);
  const payoffRef = useRef<HTMLDivElement>(null);
  const playbackControlRef = useRef<HTMLButtonElement>(null);
  const demoVideoRef = useRef<HTMLVideoElement>(null);
  const userPausedRef = useRef(false);
  const logoPlaybackStartedRef = useRef(false);
  const maskId = `${useId()}-kria-video-mask`;
  const [heroStage, setHeroStage] = useState<LandingHeroStage>("fullscreen");
  const [timelineReady, setTimelineReady] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [staticFallback, setStaticFallback] = useState(false);
  const [hasRevealedPayoff, setHasRevealedPayoff] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [activeDemoStep, setActiveDemoStep] = useState(0);
  const [isDemoPlaying, setIsDemoPlaying] = useState(false);

  const handleLogoMediaError = useCallback(() => {
    logoVideoRef.current?.pause();
    logoPlaybackStartedRef.current = false;
    setStaticFallback(true);
    setHasRevealedPayoff(true);
    setHeroStage("complete");
    setTimelineReady(true);
  }, []);

  const startLogoPlayback = useCallback(() => {
    const video = videoRef.current;
    const logoVideo = logoVideoRef.current;
    if (!video || !logoVideo) return;

    logoPlaybackStartedRef.current = true;
    logoVideo.currentTime = video.currentTime;
    const playback = logoVideo.play();
    if (playback) void playback.catch(handleLogoMediaError);
  }, [handleLogoMediaError]);

  useEffect(() => {
    const video = videoRef.current;
    const logoVideo = logoVideoRef.current;
    if (!video) return;

    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const applyMotionPreference = () => {
      setReducedMotion(media.matches);
      if (media.matches) {
        video.pause();
        logoVideo?.pause();
        setHeroStage("complete");
        setTimelineReady(true);
        setIsPlaying(false);
        return;
      }
      if (!userPausedRef.current) {
        const playback = video.play();
        if (playback) {
          void playback.catch(() => {
            logoVideo?.pause();
            setStaticFallback(true);
            setHasRevealedPayoff(true);
            setHeroStage("complete");
            setTimelineReady(true);
            setIsPlaying(false);
          });
        }
      }
    };

    applyMotionPreference();
    media.addEventListener("change", applyMotionPreference);
    return () => media.removeEventListener("change", applyMotionPreference);
  }, []);

  useEffect(() => {
    const timeout = window.setTimeout(
      () => setHasRevealedPayoff(true),
      PAYOFF_FALLBACK_MS,
    );
    return () => window.clearTimeout(timeout);
  }, []);

  useEffect(() => {
    const section = scrollSectionRef.current;
    const hero = heroRef.current;
    const fullLayer = fullVideoLayerRef.current;
    const logoLayer = logoVideoLayerRef.current;
    const mask = logoMaskRef.current;
    const maskImage = logoMaskImageRef.current;
    const foreignObject = logoForeignObjectRef.current;
    const wordmark = wordmarkRef.current;
    const payoff = payoffRef.current;
    const playbackControl = playbackControlRef.current;
    if (
      !section ||
      !hero ||
      !fullLayer ||
      !logoLayer ||
      !mask ||
      !maskImage ||
      !foreignObject ||
      !wordmark ||
      !payoff ||
      !playbackControl
    ) {
      return;
    }

    let raf = 0;

    const measureMask = () => {
      const width = hero.clientWidth;
      const height = hero.clientHeight;
      const compact = width <= 640;
      const logoWidth = compact
        ? Math.min(width * 0.88, height * 0.64)
        : Math.min(width * 0.68, height * 0.74, 660);
      const logoHeight = logoWidth * (42.8 / 97.4);
      const x = (width - logoWidth) / 2;
      const y = (height - logoHeight) / 2;

      logoLayer.setAttribute("viewBox", `0 0 ${width} ${height}`);
      mask.setAttribute("width", String(width));
      mask.setAttribute("height", String(height));
      maskImage.setAttribute("x", String(x));
      maskImage.setAttribute("y", String(y));
      maskImage.setAttribute("width", String(logoWidth));
      maskImage.setAttribute("height", String(logoHeight));
      foreignObject.setAttribute("width", String(width));
      foreignObject.setAttribute("height", String(height));
    };

    const applyProgress = (progress: number) => {
      const state = getLandingMorphState(progress);
      fullLayer.style.opacity = state.fullOpacity.toFixed(4);
      fullLayer.style.transform = `scale(${state.fullScale.toFixed(4)})`;
      fullLayer.style.clipPath = `inset(${state.insetY.toFixed(3)}% ${state.insetX.toFixed(3)}% round ${state.radius.toFixed(2)}px)`;
      logoLayer.style.opacity = state.logoOpacity.toFixed(4);
      logoLayer.style.transform = `scale(${state.logoScale.toFixed(4)})`;
      wordmark.style.opacity = state.wordmarkOpacity.toFixed(4);
      payoff.style.opacity = state.chromeOpacity.toFixed(4);
      playbackControl.style.opacity = state.chromeOpacity.toFixed(4);

      const chromeHidden = progress > 0.24;
      payoff.style.visibility = chromeHidden ? "hidden" : "";
      playbackControl.style.visibility = chromeHidden ? "hidden" : "";
      section.dataset.morphing = progress > 0.001 ? "true" : "false";
      section.dataset.morphComplete = progress >= 0.995 ? "true" : "false";
    };

    const update = () => {
      raf = 0;
      if (reducedMotion || staticFallback) {
        applyProgress(reducedMotion ? 1 : 0);
        return;
      }
      const progress = getLandingScrollProgress(
        window.scrollY,
        section.offsetTop,
        section.offsetHeight,
        window.innerHeight,
      );
      applyProgress(progress);
      if (
        progress > 0.02 &&
        !logoPlaybackStartedRef.current &&
        !videoRef.current?.paused
      ) {
        startLogoPlayback();
      }
    };

    const scheduleUpdate = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };

    const handleResize = () => {
      measureMask();
      scheduleUpdate();
    };

    measureMask();
    update();
    window.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", handleResize);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [reducedMotion, startLogoPlayback, staticFallback]);

  const handleTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    const logoVideo = logoVideoRef.current;
    if (
      logoPlaybackStartedRef.current &&
      logoVideo &&
      Number.isFinite(video.currentTime) &&
      Math.abs(logoVideo.currentTime - video.currentTime) > 0.12
    ) {
      logoVideo.currentTime = video.currentTime;
    }
    const stage = getLandingHeroStage(video.currentTime);
    setHeroStage((previous) => (previous === "complete" ? previous : stage));
    if (stage === "complete") setHasRevealedPayoff(true);
  }, []);

  const handlePlaying = useCallback(() => {
    const video = videoRef.current;
    const logoVideo = logoVideoRef.current;
    if (logoPlaybackStartedRef.current && video && logoVideo) {
      logoVideo.currentTime = video.currentTime;
      const playback = logoVideo.play();
      if (playback) void playback.catch(handleLogoMediaError);
    } else {
      const section = scrollSectionRef.current;
      if (
        section &&
        getLandingScrollProgress(
          window.scrollY,
          section.offsetTop,
          section.offsetHeight,
          window.innerHeight,
        ) > 0.02
      ) {
        startLogoPlayback();
      }
    }
    setTimelineReady(true);
    setIsPlaying(true);
  }, [handleLogoMediaError, startLogoPlayback]);

  const handlePause = useCallback(() => {
    logoVideoRef.current?.pause();
    setIsPlaying(false);
  }, []);

  const handleWaiting = useCallback(() => {
    setTimelineReady(false);
  }, []);

  const handleMediaError = useCallback(() => {
    logoVideoRef.current?.pause();
    setStaticFallback(true);
    setHasRevealedPayoff(true);
    setHeroStage("complete");
    setTimelineReady(true);
    setIsPlaying(false);
  }, []);

  const togglePlayback = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      userPausedRef.current = false;
      setStaticFallback(false);
      const logoVideo = logoVideoRef.current;
      if (logoPlaybackStartedRef.current && logoVideo) {
        logoVideo.currentTime = video.currentTime;
        const logoPlayback = logoVideo.play();
        if (logoPlayback) void logoPlayback.catch(handleLogoMediaError);
      }
      const playback = video.play();
      if (playback) {
        void playback
          .then(() => setIsPlaying(true))
          .catch(handleMediaError);
      }
      return;
    }
    userPausedRef.current = true;
    video.pause();
    logoVideoRef.current?.pause();
    setHasRevealedPayoff(true);
    setIsPlaying(false);
  }, [handleLogoMediaError, handleMediaError]);

  useEffect(() => {
    if (!reducedMotion) return;
    demoVideoRef.current?.pause();
    setActiveDemoStep(0);
    setIsDemoPlaying(false);
  }, [reducedMotion]);

  const handleDemoTimeUpdate = useCallback(() => {
    const video = demoVideoRef.current;
    if (!video) return;
    setActiveDemoStep(getProductDemoStep(video.currentTime));
  }, []);

  const toggleDemoPlayback = useCallback(() => {
    const video = demoVideoRef.current;
    if (!video) return;
    if (video.paused) {
      const playback = video.play();
      if (playback) void playback.catch(() => setIsDemoPlaying(false));
      return;
    }
    video.pause();
  }, []);

  return (
    <div className={styles.page}>
      <section
        ref={scrollSectionRef}
        className={styles.heroScroll}
        aria-labelledby="kria-life-title"
        data-stage={heroStage}
        data-playing={isPlaying}
        data-timeline-ready={timelineReady}
        data-reduced-motion={reducedMotion}
        data-static={staticFallback}
        data-payoff-visible={hasRevealedPayoff || reducedMotion || staticFallback}
      >
        <div ref={heroRef} className={styles.hero}>
          <h1 id="kria-life-title" className={styles.srOnly}>
            Kria turns the moments in your camera roll into finished short-form videos.
          </h1>

          <div ref={fullVideoLayerRef} className={styles.videoMorph} aria-hidden="true">
            <video
              ref={videoRef}
              className={styles.heroVideo}
              src={HERO_VIDEO}
              poster={HERO_POSTER}
              muted
              loop
              playsInline
              preload="metadata"
              onPlaying={handlePlaying}
              onPause={handlePause}
              onWaiting={handleWaiting}
              onTimeUpdate={handleTimeUpdate}
              onError={handleMediaError}
            />
          </div>

          <svg
            ref={logoVideoLayerRef}
            className={styles.logoVideoLayer}
            viewBox="0 0 1280 720"
            preserveAspectRatio="none"
            role="presentation"
            aria-hidden="true"
          >
            <defs>
              <mask
                ref={logoMaskRef}
                id={maskId}
                x="0"
                y="0"
                width="1280"
                height="720"
                maskUnits="userSpaceOnUse"
              >
                <rect width="100%" height="100%" fill="#000000" />
                <image
                  ref={logoMaskImageRef}
                  href={KRIA_WORDMARK}
                  preserveAspectRatio="xMidYMid meet"
                />
              </mask>
            </defs>
            <foreignObject
              ref={logoForeignObjectRef}
              x="0"
              y="0"
              width="1280"
              height="720"
              mask={`url(#${maskId})`}
            >
              <div className={styles.logoVideoViewport}>
                <video
                  ref={logoVideoRef}
                  className={styles.logoVideo}
                  src={HERO_VIDEO}
                  poster={HERO_POSTER}
                  muted
                  loop
                  playsInline
                  preload="metadata"
                  tabIndex={-1}
                  aria-hidden="true"
                  onError={handleLogoMediaError}
                />
              </div>
            </foreignObject>
          </svg>

          <svg
            ref={wordmarkRef}
            className={styles.wordmark}
            viewBox="3.8 42.5 97.4 42.8"
            role="presentation"
            aria-hidden="true"
          >
            {PRODUCTION_WORDMARK_LETTERS.map((letter) => (
              <use
                key={letter.glyph}
                href={`${KRIA_WORDMARK}#${letter.definition}`}
                transform={letter.transform}
                className={`${styles.wordmarkLetter} ${styles[letter.className]}`}
                fill="#ffffff"
              />
            ))}
          </svg>

          <div ref={payoffRef} className={styles.heroPayoff}>
            <div className={styles.heroActions}>
              <Link href="/plan" className={styles.primaryAction}>
                Create a video
              </Link>
            </div>
          </div>

          <Button
            ref={playbackControlRef}
            type="button"
            variant="ghost"
            size="icon"
            className={styles.playbackControl}
            onClick={togglePlayback}
            aria-label={isPlaying ? "Pause hero reel" : "Play hero reel"}
          >
            {isPlaying ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
          </Button>
        </div>
      </section>

      {SHOW_FOLLOWUP_SECTIONS ? (
        <>
          <section
            id="how-it-works"
            className={styles.productStory}
            aria-labelledby="product-story-title"
          >
        <header className={styles.productStoryHeader}>
          <h2 id="product-story-title">From prompt to final cut</h2>
        </header>

        <div className={styles.journeyDemo}>
          <div className={styles.journeyCopyPanel}>
            <ol className={styles.journey}>
              {PRODUCT_DEMO_STEPS.map((step, index) => (
                <li
                  key={step.title}
                  className={styles.journeyStep}
                  data-active={activeDemoStep === index}
                  aria-current={activeDemoStep === index ? "step" : undefined}
                >
                  <p className={styles.stepNumber} aria-hidden="true">
                    {String(index + 1).padStart(2, "0")} / 04
                  </p>
                  <div className={styles.stepCopy}>
                    <h3>{step.title}</h3>
                    <p>{step.body}</p>
                  </div>
                </li>
              ))}
            </ol>

            <div className={styles.journeyProgress} aria-hidden="true">
              {PRODUCT_DEMO_STEPS.map((step, index) => (
                <span key={step.title} data-active={activeDemoStep === index} />
              ))}
            </div>
          </div>

          <div className={styles.productComposition}>
            <video
              ref={demoVideoRef}
              className={styles.productDemoVideo}
              src={PRODUCT_DEMO_VIDEO}
              poster={PRODUCT_DEMO_POSTER}
              muted
              loop
              autoPlay
              playsInline
              preload="auto"
              aria-label="A real Kria session moving from uploaded clips to a generated edit, prompt revision, and ready-to-publish video"
              onTimeUpdate={handleDemoTimeUpdate}
              onPlaying={() => setIsDemoPlaying(true)}
              onPause={() => setIsDemoPlaying(false)}
              onError={() => setIsDemoPlaying(false)}
            />
            <span className={styles.demoSpeed} aria-hidden="true">
              Real product flow · 1.5×
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={styles.demoPlayback}
              onClick={toggleDemoPlayback}
              aria-label={
                isDemoPlaying ? "Pause product walkthrough" : "Play product walkthrough"
              }
            >
              {isDemoPlaying ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
            </Button>
          </div>
        </div>
          </section>

          <section className={styles.closingSection}>
            <KriaMark className={styles.closingMark} />
            <p>Make the memory. Kria will make the edit.</p>
          </section>

          <footer className={styles.footer}>
            <p>© {new Date().getFullYear()} Kria</p>
            <nav aria-label="Legal">
              <Link href="/terms">Terms of Service</Link>
              <Link href="/privacy">Privacy Policy</Link>
            </nav>
          </footer>
        </>
      ) : null}
    </div>
  );
}
