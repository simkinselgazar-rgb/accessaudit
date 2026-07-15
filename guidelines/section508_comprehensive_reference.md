# Section 508 / WCAG Comprehensive Accessibility Testing Reference
## Extracted from Section508.gov and ICT Testing Baseline (Access Board)

---

# TABLE OF CONTENTS

1. [Legal Framework](#1-legal-framework)
2. [ICT Testing Baseline - Master Test List](#2-ict-testing-baseline---master-test-list)
3. [Web-Specific Requirements](#3-web-specific-requirements)
4. [Document-Specific Requirements (All Formats)](#4-document-specific-requirements)
5. [PDF-Specific Requirements](#5-pdf-specific-requirements)
6. [Word Document (DOCX) Requirements](#6-word-document-docx-requirements)
7. [Spreadsheet (XLSX) Requirements](#7-spreadsheet-xlsx-requirements)
8. [Presentation (PPTX) Requirements](#8-presentation-pptx-requirements)
9. [Media Requirements (Audio/Video)](#9-media-requirements)
10. [Alternative Text Requirements](#10-alternative-text-requirements)
11. [Color and Contrast Requirements](#11-color-and-contrast-requirements)
12. [Tools Reference](#12-tools-reference)
13. [Training Resources](#13-training-resources)
14. [WCAG-to-Baseline Cross-Reference Table](#14-wcag-to-baseline-cross-reference-table)

---

# 1. LEGAL FRAMEWORK

## Primary Law
- **Section 508 of the Rehabilitation Act** (29 U.S.C. 794d): Federal agencies must make electronic and information technology accessible to people with disabilities
- **Revised ICT Standards**: Issued January 18, 2017; Effective January 18, 2018
- **Technical Standard**: WCAG 2.0 Level A and Level AA (harmonized with W3C WCAG 2.0)

## Scope of Content Requiring Accessibility
### Public-Facing Content
All content made available to members of the general public (websites, blogs, forms, social media, kiosks)

### Agency Official Communications (9 categories)
1. Emergency notifications
2. Administrative claim decisions
3. Program/policy announcements
4. Benefits and employment notices
5. Formal receipts
6. Surveys/questionnaires
7. Templates/forms
8. Educational/training materials
9. Intranet web pages

## Key Federal Acquisition Regulation (FAR) Sections
- FAR 39.2: ICT accessibility requirements
- FAR 7.103(q): Standards inclusion in acquisition planning
- FAR 7.105(5)(iv): Documentation of authorized exceptions/exemptions
- FAR 10.001(a)(3)(ix): Assessment of ICT meeting all standards
- FAR 11.002(f): Documentation of user disability needs
- FAR 12.202(d): Inclusion of accessibility standards in requirements
- FAR 39.203: Applicability to general ICT procurement

## Related Laws
- Americans with Disabilities Act (ADA)
- Section 255 of the Communications Act (telecom accessibility)
- 21st Century IDEA (2018) - website modernization
- 21st Century Communications and Video Accessibility Act (2010)

---

# 2. ICT TESTING BASELINE - MASTER TEST LIST

## Baseline for Web (v3.1, April 2024) - 24 Tests

| # | Baseline Test | Key Test IDs |
|---|---|---|
| 1 | Keyboard Accessible | 1.A-KeyboardAccess, 1.B-NoKeyboardTrap |
| 2 | Focus | 2.A-FocusVisible, 2.B-FocusOrder, 2.C-OnFocus |
| 3 | Non-Interference | 3.A-NonInterference |
| 4 | Repetitive Content | 4.A-BypassBlocks, 4.B-ConsistentNavigation, 4.C-ConsistentIdentification |
| 5 | User Controls | 5.A-ControlName, 5.B-ControlRole, 5.C-ControlState, 5.D-ControlValue |
| 6 | Images | 6.A-MeaningfulImage, 6.B-DecorativeImage, 6.C-Captcha, 6.D-ImageText |
| 7 | Sensory Characteristics | 7.A-Color, 7.B-SensoryCharacteristics, 7.C-AudibleCues |
| 8 | Contrast | 8.A-ContrastMinimum |
| 9 | Flashing | 9.A-Flashes |
| 10 | Forms | 10.A-FormName, 10.B-FormDescriptiveLabel, 10.C-OnInput, 10.D-ErrorIdentification, 10.E-FormHasLabel, 10.F-ErrorSuggestion, 10.G-ErrorPrevention |
| 11 | Page Titles | 11.A-PageTitled |
| 12 | Tables | 12.A-DataTableRole, 12.B-DataTableHeaderAssociation, 12.C-LayoutTable |
| 13 | Content Structure | 13.A-HeadingDescriptive, 13.B-VisHeadingProg, 13.C-ProgHeadingVisual, 13.D-List |
| 14 | Links | 14.A-LinkPurpose |
| 15 | Language | 15.A-LanguagePage, 15.B-LanguagePart |
| 16 | Audio-Only and Video-Only | 16.A-AudioOnlyTranscript, 16.B-VideoOnlyAlt, 16.C-AudioMediaAlternative, 16.D-VideoMediaAlternative |
| 17 | Synchronized Media | 17.A-MediaPlayerCCADControls, 17.B-MediaPlayerCCLevel, 17.C-MediaPlayerADLevel, 17.D-CaptionsPrerecorded, 17.E-ADPrerecorded, 17.F-CaptionsLive, 17.G-SyncMediaAlternative |
| 18 | CSS Positioning | 18.B-CSSPositionedContent |
| 19 | Frames and iFrames | 19.A-FrameTitle, 19.B-iFrameName |
| 20 | Conforming Alternate Version | 20.A-ConformingAltVersion |
| 21 | Timed Events | 21.A-TimingAdjustable, 21.B-MovingInfo, 21.C-AutoUpdate, 21.D-AudioControl |
| 22 | Resize Text | 22.A-ResizeText |
| 23 | Multiple Ways | 23.A-MultipleWays |
| 24 | Parsing | 24.A-Parsing (always passes per WCAG 2.0 Errata) |

## Baseline for Electronic Documents (v1.0, September 2024) - 24 Tests

Same structure as web but with these differences:
- Test 4 (Repetitive Content): **NOT APPLICABLE** to documents
- Test 6.C (CAPTCHA): **NOT APPLICABLE** to documents
- Test 19 (Frames/iFrames): **NOT APPLICABLE** to documents
- Test 23 (Multiple Ways): **NOT APPLICABLE** to documents
- Test 11: "Page Titles" becomes "Document Titles"
- Test 18: "CSS Positioning" becomes "Meaningful Content and Sequence" (18.A-MeaningfulContent, 18.B-MeaningfulSequence)

### Document-Specific WCAG Exceptions
Non-web documents are EXEMPT from:
1. WCAG 2.4.1 Bypass Blocks
2. WCAG 2.4.5 Multiple Ways
3. WCAG 3.2.3 Consistent Navigation
4. WCAG 3.2.4 Consistent Identification

### Document Word Substitution Rule
For non-web documents, substitute "document" for "Web page" and "page" throughout WCAG criteria.

---

# 3. WEB-SPECIFIC REQUIREMENTS

## Trusted Tester Process v5.1.3 - 20 Test Categories

| # | Test Category | Baseline Alignment |
|---|---|---|
| 1 | Conforming Alternate Version and Non-Interference | Baselines 3, 20 |
| 2 | Auto-Playing and Auto-Updating Content | Baseline 21 |
| 3 | Flashing | Baseline 9 |
| 4 | Keyboard Access and Focus | Baselines 1, 2 |
| 5 | Forms | Baseline 10 |
| 6 | Links and Buttons | Baselines 5, 14 |
| 7 | Images | Baseline 6 |
| 8 | Adjustable Time Limits | Baseline 21 |
| 9 | Repetitive Content | Baseline 4 |
| 10 | Content Structure | Baseline 13 |
| 11 | Language | Baseline 15 |
| 12 | Page Titles, Frames, and iFrames | Baselines 11, 19 |
| 13 | Sensory Characteristics and Contrast | Baselines 7, 8 |
| 14 | Tables | Baseline 12 |
| 15 | CSS Content and Positioning | Baseline 18 |
| 16 | Pre-Recorded Audio-Only, Video-Only, and Animations | Baseline 16 |
| 17 | Synchronized Media | Baseline 17 |
| 18 | Resize Text | Baseline 22 |
| 19 | Multiple Ways | Baseline 23 |
| 20 | Parsing | Baseline 24 |

## Three Testing Approaches (Section 508 Recommended)

### 1. Automated Testing
- High-volume scanning with minimal human intervention
- Cannot apply subjective judgment (alt text equivalency, etc.)
- May generate false positives
- Best for: initial error identification, developer unit testing, periodic monitoring
- Tool requirements: customizable rulesets, multiple content types, browser emulation, remediation guidance

### 2. Manual Testing
- Documented, consistent, repeatable processes using human judgment
- Required for: validating automated results, subjective assessments, high-priority content
- Trusted Tester certification is the standard manual test approach

### 3. Hybrid Testing (Recommended)
- Build accessibility during development
- Manual testing before publishing
- Automated tools for obvious errors in CI/CD
- Focus manual testing on high-traffic, poorly-performing content

---

# 4. DOCUMENT-SPECIFIC REQUIREMENTS (All Formats)

## Detailed Baseline Tests for Documents

### Baseline 1: Keyboard Accessible (Docs)
- **WCAG**: 2.1.1 Keyboard, 2.1.2 No Keyboard Trap
- **Tests**: 1.A (all functionality via keyboard), 1.B (no keyboard traps)
- **Applies to**: Interactive documents with form fields, links, embedded objects

### Baseline 2: Focus (Docs)
- **WCAG**: 2.4.3 Focus Order, 2.4.7 Focus Visible, 3.2.1 On Focus
- **Tests**: 2.A (visible focus indicator), 2.B (logical focus order), 2.C (no context change on focus)

### Baseline 5: User Controls (Docs)
- **WCAG**: 4.1.2 Name, Role, Value
- **Tests**: 5.A (non-empty accessible name), 5.B (valid role), 5.C (accurate state), 5.D (accurate value)

### Baseline 6: Images (Docs)
- **WCAG**: 1.1.1 Non-text Content, 1.4.5 Images of Text, 4.1.2 Name Role Value
- **Tests**:
  - 6.A: Meaningful images have equivalent alt text
  - 6.B: Decorative images properly marked (empty alt, artifact)
  - 6.C: CAPTCHA - NOT APPLICABLE to documents
  - 6.D: Images of text - text should be used instead where possible

### Baseline 7: Sensory Characteristics (Docs)
- **WCAG**: 1.1.1, 1.3.3, 1.4.1
- **Tests**:
  - 7.A: Color alone does not convey meaning (provide text/shape alternatives)
  - 7.B: Instructions don't rely solely on shape/size/position
  - 7.C: Audible cues have text alternatives

### Baseline 8: Contrast (Docs)
- **WCAG**: 1.4.3 Contrast (Minimum)
- **Ratios**: Standard text 4.5:1, Large text 3:1
- **Large text**: 18pt+ OR 14pt+ bold (weight >= 700)
- **Exempt**: Logotypes, disabled components, decorative elements
- **Critical**: Tools must NOT round values (4.499:1 FAILS the 4.5:1 threshold)

### Baseline 9: Flashing (Docs)
- **WCAG**: 2.3.1 Three Flashes or Below Threshold
- **Rule**: No more than 3 flashes per second, OR flash area <= 341x256px at 1024x768

### Baseline 10: Forms (Docs)
- **WCAG**: 1.1.1, 1.3.1, 2.4.6, 3.2.2, 3.3.1, 3.3.2, 3.3.3, 3.3.4, 4.1.2
- **Tests**:
  - 10.A: Form components have accessible names describing purpose
  - 10.B: Labels describe purpose with expected format
  - 10.C: No automatic context changes on input
  - 10.D: Errors identified and described in text
  - 10.E: Visible labels present during focus
  - 10.F: Error suggestions provided (unless security risk)
  - 10.G: Legal/financial submissions are reversible, validated, or confirmable

### Baseline 11: Document Titles (Docs)
- **WCAG**: 2.4.2 Page Titled
- **Test**: 11.A - Document Title property is defined and descriptive
- **Applies to all formats**: PDF Title metadata, Word document properties, etc.

### Baseline 12: Tables (Docs)
- **WCAG**: 1.3.1, 4.1.2
- **Tests**:
  - 12.A: Data tables have programmatic table role; cells and headers properly typed
  - 12.B: All data cells associated with relevant headers
  - 12.C: Layout tables do NOT use data table markup

### Baseline 13: Content Structure (Docs)
- **WCAG**: 1.3.1, 2.4.6
- **Tests**:
  - 13.A: Headings describe topic/purpose
  - 13.B: Visual headings are programmatically marked as headings with correct levels
  - 13.C: Programmatic headings also serve as visual headings (no heading markup for emphasis)
  - 13.D: Lists properly marked as bulleted/numbered/description lists

### Baseline 14: Links (Docs)
- **WCAG**: 2.4.4 Link Purpose, 4.1.2 Name Role Value
- **Test**: 14.A - Link has non-empty accessible name; purpose determinable from text, context, or structure

### Baseline 15: Language (Docs)
- **WCAG**: 3.1.1 Language of Page, 3.1.2 Language of Parts
- **Tests**:
  - 15.A: Document language property matches predominant language (IANA subtag)
  - 15.B: Passages in different languages are marked with correct language attribute

### Baseline 16: Audio-Only and Video-Only (Docs)
- **WCAG**: 1.2.1
- **Tests**: Transcripts for audio; text descriptions or audio tracks for video

### Baseline 17: Synchronized Media (Docs)
- **WCAG**: 1.2.2, 1.2.4, 1.2.5; Section 508 503.4, 503.4.1, 503.4.2
- **Tests**: Media player controls for CC/AD; accurate captions; audio descriptions for video

### Baseline 18: Meaningful Content and Sequence (Docs)
- **WCAG**: 1.3.1, 1.3.2
- **Tests**:
  - 18.A: All meaningful content exists in document body or is programmatically identified (including headers, footers, watermarks)
  - 18.B: Reading order of all content follows logical sequence preserving meaning

### Baseline 20: Conforming Alternate Version (Docs)
- **WCAG**: Conformance Requirement 1
- **Tests**: Alternate version provides same info/functionality, is current, passes all baselines, is reachable

### Baseline 21: Timed Events (Docs)
- **WCAG**: 1.4.2, 2.2.1, 2.2.2
- **Tests**: Time limits adjustable/extendable; moving content pausable; auto-updates controllable; auto-audio controllable

### Baseline 22: Resize Text (Docs)
- **WCAG**: 1.4.4
- **Test**: Text resizable to 200% without loss of content or functionality

### Baseline 24: Parsing (Docs)
- **WCAG**: 4.1.1 (deprecated in WCAG 2.2, always passes per errata)

---

# 5. PDF-SPECIFIC REQUIREMENTS

## PDF Tag Structure (Complete Reference)

### Root Tag
- `<Document>` - Main container for all other tags

### Container/Grouping Tags
- `<Part>` - Large document sections (book chapters)
- `<Sect>` - Smaller sections (sidebars, boxed content)

### Heading Tags
- `<H1>` - Document primary title
- `<H2>` - Chapter/main-level headings
- `<H3>` through `<H6>` - Progressive subheading levels

### Text Tags
- `<P>` - Body text (most common tag)
- `<BlockQuote>` - Extended quotations

### List Tags
- `<L>` - List container
- `<LI>` - List item
- `<Lbl>` - Bullet identifier (number, letter, symbol)
- `<LBody>` - Text of a list item

### Table of Contents Tags
- `<TOC>` - Table of contents container
- `<TOCI>` - Individual TOC entry

### Table Tags
- `<Table>` - Table container
- `<TR>` - Table row
- `<TH>` - Header cell
- `<TD>` - Data cell

### Inline/Character Tags
- `<Link>` - Active hyperlinks (URL, email)
- `<OBJR>` - Active component of reference link
- `<Reference>` - Internal cross-references, footnotes, TOC links
- `<Span>` - Separator for differently formatted text (italic, bold)
- `<Note>` - Footnotes, endnotes, source notes
- `<Form>` - Interactive form elements

### Figure/Formula Tags
- `<Figure>` - Graphics (logos, illustrations, photos, charts)
- `<Formula>` - Mathematical formulas
- `<Caption>` - Figure or table caption

### Special Tags
- `<Artifact>` - Decorative/non-essential content (excluded from screen reader output)

## PDF-Specific Testing Checklist
1. Document has Title metadata set (not filename)
2. Document language is set in properties
3. All content is tagged (no untagged content)
4. Tag tree reading order matches visual/logical order
5. All images have appropriate alt text or are marked as artifacts
6. Headings use proper heading tags (H1-H6) with correct hierarchy
7. Lists use proper list tags (L, LI, Lbl, LBody)
8. Tables have proper structure (Table, TR, TH, TD)
9. Table headers are marked as TH with scope
10. Links are tagged as Link with meaningful text
11. Form fields have tooltips/labels
12. Tab order of forms matches visual order
13. Bookmarks present for documents > 9 pages
14. Color contrast meets 4.5:1 (standard) / 3:1 (large)
15. No content relies solely on color
16. Scanned images have OCR text or text alternative
17. Decorative elements marked as Artifacts
18. Security settings allow assistive technology access
19. Headers/footers properly marked as artifacts or tagged content
20. Watermarks have relevant info in main content

## PDF Testing Tools
- Adobe Acrobat Pro DC (built-in accessibility checker)
- PAC (PDF Accessibility Checker)
- CommonLook PDF Validator
- axesPDF QuickFix

## PDF Training Resources
- 5-part video series: "How to Test and Remediate PDFs for Accessibility Using Adobe Acrobat DC" (63 min 51 sec)
  - Module 0: Introduction
  - Module 1: What is a PDF?
  - Module 2: Testing a PDF for Accessibility
  - Module 3: Remediating PDFs
  - Module 4: Converting Scanned Documents

---

# 6. WORD DOCUMENT (DOCX) REQUIREMENTS

## Key Requirements
1. Use built-in heading Styles (Heading 1-6) for document structure
2. Use built-in list styles (bulleted, numbered) not manual formatting
3. Provide alt text for all meaningful images
4. Mark decorative images as decorative
5. Use proper table structure with header rows identified
6. Set document language in File > Options > Language
7. Mark passages in other languages with proofing language
8. Use descriptive hyperlink text (not bare URLs)
9. Set document Title in File > Properties
10. Save with descriptive filename
11. Ensure sufficient color contrast (4.5:1 / 3:1 large)
12. Do not use color alone to convey meaning
13. Use built-in columns (not tabs/spaces for columnar layout)
14. Include text descriptions for charts and complex images
15. Use proper reading order (check in Selection Pane)
16. Ensure all content is in the main body (not only in text boxes)
17. Add headers and footers using built-in tools
18. Use built-in page numbers
19. Test with Accessibility Checker (Review > Check Accessibility)

## Word Training Resources
- 14-part video series covering all minimum steps for conformance
- Basic Authoring and Testing Guides available for Word 2016, 2013, 2010
- Printable Testing Checklists available

## Word-Specific Baseline Differences
- Document Title = Word document properties Title field
- Language = Word proofing language settings
- Reading order = determined by document content flow and text box ordering

---

# 7. SPREADSHEET (XLSX) REQUIREMENTS

## Key Requirements
1. Organize content logically - data starts in cell A1
2. Provide descriptive sheet/tab names
3. Define header rows and use them as table headers
4. Use proper data table formatting (Format as Table or Name Manager)
5. Provide alt text for all charts, images, and embedded objects
6. Mark decorative images as decorative
7. Ensure sufficient color contrast (4.5:1 / 3:1 large)
8. Do not use color alone to convey meaning (e.g., red = bad)
9. Add text labels in addition to color coding
10. Set document language
11. Set document Title in properties
12. Use descriptive hyperlink text
13. Avoid merged cells where possible (breaks screen reader navigation)
14. Provide text descriptions for complex charts
15. Use meaningful cell names/references
16. Avoid blank rows/columns used as spacers
17. Ensure logical reading order within sheets
18. Include instructions for interactive elements
19. Save with descriptive filename

## Spreadsheet-Specific Challenges
- Merged cells cause navigation issues for screen readers
- Charts embedded in spreadsheets need alt text AND underlying data table
- Multiple sheets need clear naming and navigation
- Conditional formatting using color alone fails accessibility
- Formulas referencing other sheets need clear context

## Spreadsheet Training Resources
- 12-part video series (30 min 50 sec total):
  - Module 0: Introduction
  - Module 1: Content Organization
  - Module 2-3: Contrast and Color
  - Module 4: Background Information
  - Module 5: Data Tables
  - Module 6: Alternative Text
  - Module 7: Links
  - Module 8: Multimedia
  - Module 9: Flashing Objects
  - Module 10: File Format and Naming

---

# 8. PRESENTATION (PPTX) REQUIREMENTS

## Key Requirements
1. Use built-in slide layouts (not blank slides with text boxes)
2. Use Slide Master for consistent layout
3. Set reading order in Selection Pane for each slide
4. Provide alt text for all images, charts, SmartArt
5. Mark decorative images as decorative
6. Use built-in table tools for data tables
7. Ensure sufficient color contrast (4.5:1 / 3:1 large)
8. Do not use color alone to convey meaning
9. Use descriptive hyperlink text
10. Set document language
11. Set presentation Title in properties
12. Add slide titles to every slide (unique, descriptive)
13. Use built-in list formatting
14. Use built-in columns (not text boxes side by side)
15. Include speaker notes for additional context
16. Provide text descriptions for complex diagrams
17. Avoid auto-advancing slides or provide user controls
18. Ensure embedded video has captions
19. Ensure embedded audio has transcripts
20. Avoid flashing/blinking content
21. Save with descriptive filename
22. Consider providing a Word/PDF alternative for complex presentations

## Presentation-Specific Challenges
- Reading order is per-slide and must be manually set in Selection Pane
- Animations and transitions can cause accessibility issues
- SmartArt needs alt text describing the concept, not each piece
- Embedded media must meet all media accessibility requirements
- Auto-playing content must be controllable
- Slide titles are critical for navigation (screen readers use them)

## Presentation Training Resources
- 14-part video series (43 min 54 sec):
  - Module 0: Introduction
  - Module 1: Layout Design
  - Module 2: Contrast Ratios
  - Module 3: Color Descriptions
  - Module 4: Columns
  - Module 5: Lists
  - Module 6: Data Tables
  - Module 7: Alternative Text
  - Module 8: Links
  - Module 9: Background Information
  - Module 10: Language Formatting
  - Module 11: Multimedia Descriptions
  - Module 12: Flashing Objects
  - Module 13: File Format and Naming
- Additional: "Creating PowerPoint Templates" (8 min 18 sec)

---

# 9. MEDIA REQUIREMENTS (Audio/Video)

## Caption Requirements
### When Required
- All pre-recorded synchronized media (video with audio)
- All live synchronized media
- NOT required for media alternatives clearly labeled as such

### Caption Quality Standards
- Synchronized with corresponding audio
- Include all dialogue AND important sounds
- Speaker identification when multiple speakers
- Sound effects in brackets: [door opens]
- Indicate speaker emotion/tone when meaning-bearing
- Include language descriptors: (in Spanish)

### Caption Formatting
- Sans serif font (Helvetica or Arial preferred)
- Default 18pt, white text on black translucent background
- Maximum 2 lines, 45 characters per line
- Centered in lower third of screen
- No scrolling, flashing, or distracting animation
- Sufficient on-screen duration for reading

### Auto-Captions
- Current auto-captioning does NOT meet Section 508 standards
- Must always be edited for accuracy

## Audio Description Requirements
- Required for all pre-recorded video content
- Must describe actions, characters, scene changes, on-screen text
- Controls at same menu level as volume/program selection

## Transcript Requirements
- Required for audio-only content
- Required for video-only content
- Must be in accessible format (web page, plain text, Word)
- Located in same place as original content
- Include dialogue, relevant sounds, speaker identification

## Media Player Requirements (Section 508: 503.4)
- Must have user controls for captions and audio descriptions
- Caption controls at same menu level as volume/program selection (503.4.1)
- Audio description controls at same menu level as volume/program selection (503.4.2)

---

# 10. ALTERNATIVE TEXT REQUIREMENTS

## General Rules
- Short and to the point
- Communicate same information as visual content
- Focus on relevant information, not visual appearance
- Avoid redundancy with surrounding text
- Match document language

## By Image Type

### Photos/Portraits
- Describe relevant content, not appearance
- Good: "Dr. Martin Luther King Jr."
- Bad: "Black and white photo of Dr. Martin Luther King Jr. wearing a suit"

### Images Containing Text
- Include text word-for-word when possible
- Better to use actual text instead of images

### Logos
- NEVER decorative
- Describe symbols/graphics AND include text word-for-word
- Example: "GSA logo with text: Section508.gov Buy. Build. Be Accessible"

### Decorative Images
- Mark as decorative (empty alt text)
- Prevents screen readers from reading filenames

### Charts/Graphs/Diagrams
- Identify chart type (pie chart, bar graph, etc.)
- Describe trends and relationships
- For complex images: short alt text + link to data table
- Flowcharts/org charts: describe action sequence or provide text alternative

### Form Elements/Controls
- Alt text conveys function: "Next arrow button" not "Right arrow"
- Required field indicators: "Required" not "Asterisk"

### Signatures
- Format: "Signature: [Name]"
- Mark scanned signatures as figure with alt text

### Watermarks
- Not read by screen readers
- Add relevant info to main content
- Use low contrast; avoid if possible

### CAPTCHA (Web Only)
- Must have text alternative describing purpose
- Must provide alternate modality (audio CAPTCHA for visual)

## Common Mistakes to Avoid
- Too brief or too lengthy
- Describing appearance instead of relevance
- Repeating main text content
- Using filenames as descriptions
- Computer-generated descriptions lacking context
- Language mismatch with document

---

# 11. COLOR AND CONTRAST REQUIREMENTS

## Contrast Ratios
| Text Type | Minimum Ratio |
|---|---|
| Standard text (any size below large) | 4.5:1 |
| Large text (18pt+ OR 14pt+ bold/700 weight) | 3:1 |
| 18pt = 24px, 14pt = 18.5px | |

## Exemptions from Contrast Requirements
- Logotypes and brand names
- Inactive/disabled interface components
- Pure decorative elements
- Text within photographs containing significant other visual content

## Critical Testing Notes
- Tools must NOT round values (4.499:1 FAILS the 4.5:1 threshold)
- Test hover and selection states
- Read-only components MUST meet contrast requirements
- Disabled inputs are exempt

## Color-Only Information
- NEVER use color as the sole means of conveying information
- Always provide additional visual differentiation (shape, pattern, text label)
- OR provide 3:1 contrast ratio between colors used for differentiation
- Examples: error states, required fields, status indicators, chart data series

---

# 12. TOOLS REFERENCE

## Official Section 508 Tools

### ART - Accessibility Requirements Tool
- URL: https://www.section508.gov/art/
- Purpose: Generate Section 508 requirements for ICT procurements
- API: https://art-api.section508.gov/

### ACR Editor - Accessibility Conformance Report Editor
- URL: https://acreditor.section508.gov/
- Purpose: Create accessibility conformance reports

### SRT - Solicitation Review Tool
- URL: https://www.section508.gov/buy/solicitation-review-tool/
- Purpose: Review solicitations for accessibility requirements

### SCRT - Section 508 Compliance Reporting Tool
- Purpose: Report Trusted Tester results

## Testing Tools

### ANDI (Accessible Name & Description Inspector)
- URL: https://www.ssa.gov/accessibility/andi/help/install.html
- Developer: Social Security Administration
- Type: Free open-source bookmarklet (no plugin required)
- Tests: Accessible names/descriptions, color contrast, focus, links, buttons, images, headings, lists, page language, structure, tables, CSS content, live regions
- 18 training modules available

### Color Contrast Analyzer (CCA)
- URL: https://www.tpgi.com/color-contrast-checker/
- Developer: TPGi (The Paciello Group)
- Type: Free open-source desktop application
- Platforms: Windows and macOS
- Tests: Text-to-background contrast ratios

### WebAIM Contrast Checker
- URL: https://webaim.org/resources/contrastchecker/
- Type: Web-based tool
- Tests: Text and background color contrast ratios

### Browser Developer Tools
- Chrome: developer.chrome.com/docs/devtools
- Edge: microsoft.com/edge/devtools
- Firefox: firefox-source-docs.mozilla.org/devtools-user/
- Safari: developer.apple.com/safari/tools/

### W3C Resources
- Alt Decision Tree: https://www.w3.org/WAI/tutorials/images/decision-tree/
- WCAG Understanding docs: https://www.w3.org/WAI/WCAG22/Understanding/

---

# 13. TRAINING RESOURCES

## Online Courses (Section508.gov)
1. Accessibility of ICT for Government Executives
2. Microsoft Word & Accessibility Best Practices
3. Micro-Purchases and Section 508 Requirements
4. Procuring Section 508 Conformant ICT Products and Services
5. Section 508: What Is It and Why Is It Important?
6. Soliciting and Evaluating Accessibility Conformance Reports

## Certification Programs
- **DHS Trusted Tester v5.1.3**: https://training.section508testing.net/
  - Self-enrollment portal
  - Interagency standardized testing certification

## Video Training Series
| Series | Modules | Duration |
|---|---|---|
| ANDI Training | 18 modules | Various |
| Document Accessibility (Word) | 14 modules | Various |
| Presentation Accessibility (PowerPoint) | 14 modules + template creation | 43 min 54 sec |
| Spreadsheet Accessibility (Excel) | 12 modules | 30 min 50 sec |
| PDF Accessibility | 5 modules | 63 min 51 sec |
| ART Training | 5 modules | Various |

## Reference Materials
- Accessibility Playbooks: https://www.section508.gov/manage/playbooks/
- ACR Library: https://www.section508.gov/accessibility-conformance-reports/
- Glossary: https://www.section508.gov/tools/glossary/
- WCAG 2.0: https://www.w3.org/TR/WCAG20/
- ICT Testing Baseline for Web v3.1: https://ictbaseline.access-board.gov/
- ICT Testing Baseline for Documents v1.0: https://ictbaseline.access-board.gov/document-baselines/

---

# 14. WCAG-TO-BASELINE CROSS-REFERENCE TABLE

## Complete Mapping: WCAG Success Criteria to Baseline Tests

| WCAG SC | SC Name | Baseline Test(s) | Test ID(s) |
|---|---|---|---|
| 1.1.1 | Non-text Content | 6 (Images), 7 (Sensory), 10 (Forms) | 6.A, 6.B, 7.C, 10.A |
| 1.2.1 | Audio-only/Video-only | 16 (Audio/Video) | 16.A, 16.B, 16.C, 16.D |
| 1.2.2 | Captions (Prerecorded) | 17 (Sync Media) | 17.D, 17.G |
| 1.2.4 | Captions (Live) | 17 (Sync Media) | 17.F |
| 1.2.5 | Audio Description | 17 (Sync Media) | 17.E, 17.G |
| 1.3.1 | Info and Relationships | 10 (Forms), 12 (Tables), 13 (Structure), 18 (Content/Sequence) | 10.A, 12.B, 13.B, 13.C, 13.D, 18.A |
| 1.3.2 | Meaningful Sequence | 18 (CSS/Sequence) | 18.B |
| 1.3.3 | Sensory Characteristics | 7 (Sensory) | 7.B |
| 1.4.1 | Use of Color | 7 (Sensory) | 7.A |
| 1.4.2 | Audio Control | 21 (Timed Events) | 21.D |
| 1.4.3 | Contrast (Minimum) | 8 (Contrast) | 8.A |
| 1.4.4 | Resize Text | 22 (Resize) | 22.A |
| 1.4.5 | Images of Text | 6 (Images) | 6.D |
| 2.1.1 | Keyboard | 1 (Keyboard) | 1.A |
| 2.1.2 | No Keyboard Trap | 1 (Keyboard) | 1.B |
| 2.2.1 | Timing Adjustable | 21 (Timed Events) | 21.A |
| 2.2.2 | Pause, Stop, Hide | 21 (Timed Events) | 21.B, 21.C |
| 2.3.1 | Three Flashes | 9 (Flashing) | 9.A |
| 2.4.1 | Bypass Blocks | 4 (Repetitive Content) | 4.A |
| 2.4.2 | Page/Document Titled | 11 (Titles) | 11.A |
| 2.4.3 | Focus Order | 2 (Focus) | 2.B |
| 2.4.4 | Link Purpose (In Context) | 14 (Links) | 14.A |
| 2.4.5 | Multiple Ways | 23 (Multiple Ways) | 23.A |
| 2.4.6 | Headings and Labels | 10 (Forms), 13 (Structure) | 10.B, 13.A |
| 2.4.7 | Focus Visible | 2 (Focus) | 2.A |
| 3.1.1 | Language of Page/Document | 15 (Language) | 15.A |
| 3.1.2 | Language of Parts | 15 (Language) | 15.B |
| 3.2.1 | On Focus | 2 (Focus) | 2.C |
| 3.2.2 | On Input | 10 (Forms) | 10.C |
| 3.2.3 | Consistent Navigation | 4 (Repetitive Content) | 4.B |
| 3.2.4 | Consistent Identification | 4 (Repetitive Content) | 4.C |
| 3.3.1 | Error Identification | 10 (Forms) | 10.D |
| 3.3.2 | Labels or Instructions | 10 (Forms) | 10.E |
| 3.3.3 | Error Suggestion | 10 (Forms) | 10.F |
| 3.3.4 | Error Prevention | 10 (Forms) | 10.G |
| 4.1.1 | Parsing | 24 (Parsing) | 24.A (always passes) |
| 4.1.2 | Name, Role, Value | 5 (Controls), 6 (Images), 10 (Forms), 12 (Tables), 14 (Links), 19 (Frames) | 5.A-D, 6.A, 10.A, 12.A, 12.C, 14.A, 19.A-B |
| 508:503.4 | User Controls for CC/AD | 17 (Sync Media) | 17.A |
| 508:503.4.1 | Caption Controls | 17 (Sync Media) | 17.B |
| 508:503.4.2 | Audio Description Controls | 17 (Sync Media) | 17.C |
| CR5 | Non-Interference | 3 (Non-Interference) | 3.A |

## WCAG Criteria NOT Applicable to Documents
| WCAG SC | SC Name | Reason |
|---|---|---|
| 2.4.1 | Bypass Blocks | Documents don't have repeated navigation blocks |
| 2.4.5 | Multiple Ways | Documents are single entities, not page sets |
| 3.2.3 | Consistent Navigation | Documents don't have repeated navigation |
| 3.2.4 | Consistent Identification | Documents don't have cross-page components |

---

# 15. ELECTRONIC SIGNATURES REQUIREMENTS

## PDF Forms
- Enable keyboard navigation
- Logical tab order matching visual order
- Instructions and cues for form completion
- Tooltips matching labels
- Scanned signatures: mark as figure with alt text "Signature: [Name]"

## CAPTCHA in Signature Flows
- Text alternative describing purpose
- Alternate modality (audio for visual CAPTCHA)

---

# 16. PRIORITIZATION GUIDANCE

Per Section508.gov, organizations should prioritize content for accessibility review based on:
1. **Size of target audience** (largest audience first)
2. **Frequency of user access** (most-accessed content first)
3. **Criticality of content** (essential services first)

---

*Sources: Section508.gov, ICT Testing Baseline for Web v3.1 (Access Board), ICT Testing Baseline for Electronic Documents v1.0 (Access Board), DHS Trusted Tester v5.1.3*
*Last compiled: 2026-03-28*
