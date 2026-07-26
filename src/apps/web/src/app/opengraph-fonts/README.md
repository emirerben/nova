`Fraunces-Social-Bold.ttf` is a static 700-weight, 72-optical-size instance
derived from `public/fonts/Fraunces-Bold.ttf`. Next.js's social-image renderer
cannot parse the source font's multi-axis `fvar` table, so the generated image
uses this deterministic static instance without loading a network font.
