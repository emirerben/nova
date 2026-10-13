import CoreText
import KriaMediaEngine
import SwiftUI
import UIKit

/// KRI-646: arc ("rainbow") text on a slide. Slides draw text with SwiftUI `Text`, which cannot follow a
/// curve, so a bent element is laid out with CoreText instead (same wrap rule, same `TextBend` geometry
/// as the video painter) and drawn glyph by glyph in a `Canvas`. The editor preview and the photo / video
/// export share `SlidePostTextLayerView`, so they all get the same arc.
struct SlidePostBentTextBlock: View {
    let layout: SlidePostBentLayout
    let fill: Color
    /// Outline radius in points (0 = none), matching the straight text's 8-copy outline.
    let stroke: CGFloat

    var body: some View {
        Canvas { context, _ in
            context.withCGContext { cg in
                // CoreText draws y-up; the layout is y-up with its origin at the block's bottom-left.
                cg.translateBy(x: 0, y: layout.size.height)
                cg.scaleBy(x: 1, y: -1)
                if stroke > 0 { draw(cg, mode: .stroke, color: UIColor.black.cgColor) }
                draw(cg, mode: .fill, color: UIColor(fill).cgColor)
            }
        }
        .frame(width: layout.size.width, height: layout.size.height)
        .accessibilityHidden(true)
    }

    private func draw(_ cg: CGContext, mode: CGTextDrawingMode, color: CGColor) {
        cg.saveGState(); defer { cg.restoreGState() }
        cg.setTextDrawingMode(mode)
        cg.setFillColor(color); cg.setStrokeColor(color)
        // The centred stroke is half hidden under the fill pass, so doubling it matches the outline radius.
        cg.setLineWidth(stroke * 2); cg.setLineJoin(.round)
        for glyph in layout.glyphs {
            cg.saveGState()
            cg.concatenate(glyph.transform)
            CTFontDrawGlyphs(glyph.font, [glyph.id], [glyph.position], 1, cg)
            cg.restoreGState()
        }
    }
}

/// Where every glyph of a bent slide text goes, in a y-up space whose origin is the bottom-left of the
/// bent ink (plus outline padding). Pure and cheap enough to rebuild per body evaluation.
struct SlidePostBentLayout {
    struct Placed { let font: CTFont; let id: CGGlyph; let position: CGPoint; let transform: CGAffineTransform }
    let size: CGSize
    let glyphs: [Placed]

    private struct Raw { let font: CTFont; let id: CGGlyph; let position: CGPoint; let advance: Double; let box: CGRect }

    /// `maxWidth` nil = no wrapping (the text's own lines). `alignment` is "left" / "center" / "right".
    static func make(text: String, font: CTFont, maxWidth: CGFloat?, alignment: String, lineSpacing: Double,
                     bend: Double, padding: CGFloat) -> SlidePostBentLayout? {
        guard !TextBend.isStraight(bend) else { return nil }
        let attributes: [NSAttributedString.Key: Any] = [.init(kCTFontAttributeName as String): font]
        var values: [String] = []
        for paragraph in text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n") {
            guard let maxWidth, !paragraph.isEmpty else { values.append(paragraph); continue }
            let source = paragraph as NSString
            let typesetter = CTTypesetterCreateWithAttributedString(NSAttributedString(string: paragraph, attributes: attributes))
            var offset = 0
            while offset < source.length {
                let count = min(CTTypesetterSuggestLineBreak(typesetter, offset, Double(maxWidth)), source.length - offset)
                guard count > 0 else { return nil }
                values.append(source.substring(with: NSRange(location: offset, length: count)).trimmingCharacters(in: .whitespaces))
                offset += count
            }
        }
        guard !values.isEmpty, values.count <= 100 else { return nil }
        let lines = values.map { CTLineCreateWithAttributedString(NSAttributedString(string: $0.isEmpty ? " " : $0, attributes: attributes)) }
        let widths = lines.map { CTLineGetTypographicBounds($0, nil, nil, nil) }
        let widest = widths.max() ?? 0
        let step = (CTFontGetAscent(font) + CTFontGetDescent(font) + CTFontGetLeading(font)) * lineSpacing
        var raws: [[Raw]] = []
        for (index, line) in lines.enumerated() {
            let x: Double = alignment == "left" ? 0 : (alignment == "right" ? widest - widths[index] : (widest - widths[index]) / 2)
            let baseline = -Double(index) * step
            var glyphs: [Raw] = []
            for run in CTLineGetGlyphRuns(line) as! [CTRun] {
                let count = CTRunGetGlyphCount(run)
                guard count > 0 else { continue }
                let runFont = (CTRunGetAttributes(run) as NSDictionary)[kCTFontAttributeName] as! CTFont
                var ids = [CGGlyph](repeating: 0, count: count), points = [CGPoint](repeating: .zero, count: count)
                var advances = [CGSize](repeating: .zero, count: count), boxes = [CGRect](repeating: .zero, count: count)
                CTRunGetGlyphs(run, CFRange(location: 0, length: 0), &ids)
                CTRunGetPositions(run, CFRange(location: 0, length: 0), &points)
                CTRunGetAdvances(run, CFRange(location: 0, length: 0), &advances)
                CTFontGetBoundingRectsForGlyphs(runFont, .default, ids, &boxes, count)
                for i in 0..<count {
                    glyphs.append(Raw(font: runFont, id: ids[i], position: CGPoint(x: points[i].x + x, y: baseline),
                                      advance: Double(advances[i].width), box: boxes[i]))
                }
            }
            raws.append(glyphs)
        }
        let transforms = TextBend.transforms(lines: raws.enumerated().map { index, glyphs in
            TextBend.Line(baselineY: -Double(index) * step, fontSize: CTFontGetSize(font),
                          glyphs: glyphs.map { TextBend.Glyph(x: Double($0.position.x), advance: $0.advance) })
        }, bend: bend)
        var ink = CGRect.null
        for (glyphs, lineTransforms) in zip(raws, transforms) {
            for (glyph, transform) in zip(glyphs, lineTransforms) {
                ink = ink.union(glyph.box.offsetBy(dx: glyph.position.x, dy: glyph.position.y).applying(transform))
            }
        }
        guard !ink.isNull, ink.width.isFinite, ink.height.isFinite else { return nil }
        let origin = CGAffineTransform(translationX: padding - ink.minX, y: padding - ink.minY)
        let placed = zip(raws, transforms).flatMap { glyphs, lineTransforms in
            zip(glyphs, lineTransforms).map { glyph, transform in
                Placed(font: glyph.font, id: glyph.id, position: glyph.position, transform: transform.concatenating(origin))
            }
        }
        return SlidePostBentLayout(size: CGSize(width: ceil(ink.width + 2 * padding), height: ceil(ink.height + 2 * padding)), glyphs: placed)
    }
}

extension SlidePostTextElement {
    static let bendKey = "bend"
    /// KRI-646: -100...100, positive bows the text up like a rainbow. Lives in `extra` like rotation.
    var bend: Double { extra[Self.bendKey]?.numberValue ?? 0 }
}
