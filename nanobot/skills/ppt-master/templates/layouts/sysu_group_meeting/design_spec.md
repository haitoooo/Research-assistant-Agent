---
template_id: sysu_group_meeting
category: scenario
summary: Sun Yat-sen University lab meeting deck for paper sharing, recent work, next plans, and closing discussion.
keywords: [SYSU, lab meeting, paper sharing, research progress, minimal academic]
primary_color: "#014723"
canvas_format: ppt169
replication_mode: standard
placeholders:
  01_cover: ["{{TITLE}}", "{{SUBTITLE}}", "{{AUTHOR}}", "{{DATE}}"]
  02_paper_sharing: ["{{PAGE_TITLE}}", "{{PAPER_TITLE}}", "{{PAPER_META}}", "{{KEY_MESSAGE}}", "{{CONTENT_AREA}}"]
  03_recent_work: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{CONTENT_AREA}}", "{{SOURCE}}"]
  04_next_plan: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{CONTENT_AREA}}"]
  05_ending: ["{{THANK_YOU}}", "{{ENDING_SUBTITLE}}", "{{CONTACT_INFO}}"]
---

# SYSU Group Meeting - Design Specification

## I. Template Overview

Reusable group-meeting template for Sun Yat-sen University research updates. It follows the supplied `template.pptx` reference: white academic content pages, a restrained dark-green rule, and cover/ending pages anchored by a campus photograph with a deep-green gradient overlay.

## II. Color Scheme

| Role | HEX | Usage |
| --- | --- | --- |
| Primary green | `#014723` | Cover overlay, major emphasis |
| Dark green | `#084A27` | Section rules, ending headline |
| Background | `#FFFFFF` | Content pages |
| Light text | `#F2F2F2` | Cover text over photo |
| Main text | `#000000` | Body content |
| Secondary text | `#595959` | Section labels, metadata |
| Soft panel | `#F7F9F7` | Light content areas |
| Divider | `#D9E2DC` | Tables and subtle separators |

## III. Typography

- Title/body stack: `"Microsoft YaHei", Arial, sans-serif`
- Code stack: `Consolas, "Courier New", monospace`
- Body baseline: 22px for dense academic pages.

## IV. Signature Design Elements

- Cover uses a horizontal campus photo crop from `campus_bg.jpeg`, with a left-to-right green gradient overlay and a centered SYSU mark.
- Section/content pages use a compact top-left label and a long dark-green horizontal rule around y=88.
- Content pages remain deliberately sparse: the template provides content zones, not heavy decorative frames.
- Ending page uses the upper campus photo crop and centered green thank-you text, matching the reference deck's closing rhythm.

## V. Page Roster

| File | Role | Visual character |
| --- | --- | --- |
| `01_cover.svg` | Cover | Campus photo band, dark-green gradient overlay, centered title and SYSU identity. |
| `02_paper_sharing.svg` | Paper sharing | Top rule, paper metadata block, key-takeaway strip, and large paper/figure content area. |
| `03_recent_work.svg` | Recent work | Top rule, two-column progress layout for current experiments, results, issues, or screenshots. |
| `04_next_plan.svg` | Next plan | Top rule, three-step plan cards with risk/priority emphasis and a decision area. |
| `05_ending.svg` | Ending | Upper campus photo crop, centered SYSU mark and thank-you message, contact/discussion line. |

## VI. Assets

| File | Usage |
| --- | --- |
| `campus_bg.jpeg` | Cover and ending photo crop. |
| `sysu_mark.png` | Cover SYSU mark. |
| `sysu_mark_wide.png` | Ending SYSU mark. |

## VII. Placeholder Overrides

This scenario template keeps the standard cover/content/ending placeholders and adds paper-specific slots (`{{PAPER_TITLE}}`, `{{PAPER_META}}`) for weekly paper sharing.
