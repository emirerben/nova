import { readFile } from "node:fs/promises";
import path from "node:path";

import { ImageResponse } from "next/og";

import { KriaMarkSvg } from "@/components/KriaMark";
import { APEX_WEB_ORIGIN, BRAND_NAME } from "@/lib/brand";
import { SOCIAL_IMAGE_SIZE } from "@/lib/site-metadata";

const frauncesBold = readFile(
  path.join(
    process.cwd(),
    "src/app/opengraph-fonts/Fraunces-Social-Bold.ttf",
  ),
);
const interMedium = readFile(
  path.join(process.cwd(), "public/fonts/Inter-Medium.ttf"),
);
const displayDomain = new URL(APEX_WEB_ORIGIN).hostname;

export async function GET() {
  const [displayFont, bodyFont] = await Promise.all([
    frauncesBold,
    interMedium,
  ]);

  return new ImageResponse(
    (
      <div
        style={{
          position: "relative",
          display: "flex",
          width: "100%",
          height: "100%",
          overflow: "hidden",
          background: "#fafaf8",
          color: "#0c0c0e",
          padding: "64px 72px",
          fontFamily: "Inter",
        }}
      >
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 52,
            display: "flex",
            height: 1,
            background: "#e4e4e7",
          }}
        />

        <div
          style={{
            position: "relative",
            display: "flex",
            flexDirection: "column",
            justifyContent: "space-between",
            width: "100%",
            zIndex: 1,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 16,
              fontSize: 31,
              letterSpacing: "-0.03em",
              fontWeight: 600,
            }}
          >
            <div
              style={{
                display: "flex",
                width: 18,
                height: 18,
                borderRadius: 999,
                background: "#65a30d",
              }}
            />
            {BRAND_NAME}
          </div>

          <div
            style={{
              display: "flex",
              flexDirection: "column",
              maxWidth: 780,
              marginTop: 44,
            }}
          >
            <div
              style={{
                display: "flex",
                fontFamily: "Fraunces",
                fontSize: 94,
                lineHeight: 0.95,
                letterSpacing: "-0.055em",
              }}
            >
              Your content career,
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 20,
                marginTop: 8,
              }}
            >
              <span
                style={{
                  display: "flex",
                  fontFamily: "Fraunces",
                  fontSize: 94,
                  lineHeight: 0.95,
                  letterSpacing: "-0.055em",
                }}
              >
                on autopilot.
              </span>
              <span
                style={{
                  display: "flex",
                  width: 72,
                  height: 10,
                  borderRadius: 999,
                  background: "#65a30d",
                  marginTop: 34,
                }}
              />
            </div>
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 18,
              fontSize: 24,
              color: "#3f3f46",
            }}
          >
            Ideas. Filming guides. Finished videos.
            <span style={{ color: "#0c0c0e" }}>{displayDomain}</span>
          </div>
        </div>

        <KriaMarkSvg
          idPrefix="social-card-mark"
          style={{
            position: "absolute",
            right: 74,
            top: 62,
            width: 168,
            height: 154,
            color: "#65a30d",
            zIndex: 2,
          }}
        />
      </div>
    ),
    {
      ...SOCIAL_IMAGE_SIZE,
      fonts: [
        {
          name: "Fraunces",
          data: displayFont,
          style: "normal",
          weight: 700,
        },
        {
          name: "Inter",
          data: bodyFont,
          style: "normal",
          weight: 500,
        },
      ],
    },
  );
}
