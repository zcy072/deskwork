---
name: theme-factory
description: Toolkit for styling artifacts with a theme. These artifacts can be slides, docs, reportings, HTML landing pages, etc. There are 10 pre-set themes with colors/fonts that you can apply to any artifact that has been creating, or can generate a new theme on-the-fly. Use only when the user asks about a theme, color scheme, visual style or consistent look (or wants to choose one); for ordinary document generation without a style request, do not load this skill and pick colors as the format's own skill says.
license: Complete terms in LICENSE.txt
---

> Modified for Deskwork: narrowed the trigger in `description` and added "Deskwork Notes" at the end. The rest is the upstream text from anthropics/skills.

# Theme Factory Skill

This skill provides a curated collection of professional font and color themes themes, each with carefully selected color palettes and font pairings. Once a theme is chosen, it can be applied to any artifact.

## Purpose

To apply consistent, professional styling to presentation slide decks, use this skill. Each theme includes:
- A cohesive color palette with hex codes
- Complementary font pairings for headers and body text
- A distinct visual identity suitable for different contexts and audiences

## Usage Instructions

To apply styling to a slide deck or other artifact:

1. **Show the theme showcase**: Display the `theme-showcase.pdf` file to allow users to see all available themes visually. Do not make any modifications to it; simply show the file for viewing.
2. **Ask for their choice**: Ask which theme to apply to the deck
3. **Wait for selection**: Get explicit confirmation about the chosen theme
4. **Apply the theme**: Once a theme has been chosen, apply the selected theme's colors and fonts to the deck/artifact

## Themes Available

The following 10 themes are available, each showcased in `theme-showcase.pdf`:

1. **Ocean Depths** - Professional and calming maritime theme
2. **Sunset Boulevard** - Warm and vibrant sunset colors
3. **Forest Canopy** - Natural and grounded earth tones
4. **Modern Minimalist** - Clean and contemporary grayscale
5. **Golden Hour** - Rich and warm autumnal palette
6. **Arctic Frost** - Cool and crisp winter-inspired theme
7. **Desert Rose** - Soft and sophisticated dusty tones
8. **Tech Innovation** - Bold and modern tech aesthetic
9. **Botanical Garden** - Fresh and organic garden colors
10. **Midnight Galaxy** - Dramatic and cosmic deep tones

## Theme Details

Each theme is defined in the `themes/` directory with complete specifications including:
- Cohesive color palette with hex codes
- Complementary font pairings for headers and body text
- Distinct visual identity suitable for different contexts and audiences

## Application Process

After a preferred theme is selected:
1. Read the corresponding theme file from the `themes/` directory
2. Apply the specified colors and fonts consistently throughout the deck
3. Ensure proper contrast and readability
4. Maintain the theme's visual identity across all slides

## Create your Own Theme
To handle cases where none of the existing themes work for an artifact, create a custom theme. Based on provided inputs, generate a new theme similar to the ones above. Give the theme a similar name describing what the font/color combinations represent. Use any basic description provided to choose appropriate colors/fonts. After generating the theme, show it for review and verification. Following that, apply the theme as described above.

## Deskwork Notes

These override the steps above where they differ.

- **When to ask:** only if the user asked to choose a theme or said nothing about which one. If they already described a style ("深色科技风", "公司蓝"), pick or create the closest theme yourself and say which one you used.
- **Showing the showcase:** `present_files(paths=["/workspace/skills/theme-factory/theme-showcase.pdf"])` delivers it to the user (a 10-page PDF, one theme per page). Do not edit it.
- **Asking:** `ask_user_input` takes at most 5 options, so offer the 3-4 themes that best fit the topic (recommended first) plus "看展示图后自己选" / "生成新主题". Wait for the answer before applying anything.
- **Fonts:** the theme fonts are Latin fonts. The sandbox has DejaVu Sans/Serif (FreeSans/FreeSerif are not installed and fall back to DejaVu) and Noto Sans/Serif CJK SC for Chinese. In docx/pptx, keep the theme font for Latin text and set the East Asian font for Chinese text (e.g. "Microsoft YaHei" for files the user opens on Windows); in HTML, matplotlib or PDF output, use a font installed in the sandbox (`fc-list :lang=zh`) so Chinese characters render.
- **Contrast:** several themes pair a dark background with light text. Check text contrast on every slide/page; if a palette's light colors are too pale for body text on white, use the darkest color for text.
