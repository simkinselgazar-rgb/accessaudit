# Section 508 AED-COP Training Video Content - Comprehensive Report

Extracted from Section508.gov training pages. Organized by document type with all module descriptions, transcripts, step-by-step instructions, testing procedures, and requirements.

---

## PART 1: MICROSOFT WORD DOCUMENTS (DOCX Series)

### Series Overview
- **Series Title:** "How to Make an Accessible Document in Microsoft Word"
- **Total Duration:** 59 minutes 21 seconds
- **Parts:** 14-Part Video Series (Modules 0-13)
- **Created By:** Accessible Electronic Document Community of Practice (AED-COP) and U.S. Department of Transportation
- **Based On:** Section 508 Basic Authoring and Testing Guide for Microsoft Word

---

### DOCX Module 0: Introduction & Background

**Description:** Introduces the AED-COP and covers topics in the video series for creating accessible documents.

**Key Content from Transcript:**
- Federal agencies create millions of documents each year
- Making documents accessible is required by federal law
- Subject matter experts from several federal agencies developed the AED-COP with goals of: improving accessible content on federal websites, advancing the field of accessibility, and creating reusable accessibility information and artifacts
- The group created the "Section 508 Basic Authoring and Testing Guide, Microsoft Word" identifying minimum steps for Section 508 accessibility

**Module Structure (applies to all modules):**
Each requirement has a related module demonstrating:
1. How to author accessibly
2. How to check your work
3. How to identify inaccessible content
4. How to identify accessible content

**Scope Limitations:**
- Does NOT cover documents with macros (.docm or .dotm files)
- Does NOT cover documents with form fields
- Macro-enabled documents contain programmed formatting and are better tested with a software application test process
- References DHS Trusted Tester Process (http://www.dhs.gov) for additional testing

**Resources:** Section 508 Basic Authoring and Testing Guide downloadable from http://www.section508.gov

---

### DOCX Module 1: Save as a Word Document (.docx) with a Descriptive Filename

**Description:** A descriptive file name that identifies the document or its purpose is required, as it helps everyone locate, open, and switch between documents. The document must be in the .docx format.

**How to Author Accessibly:**
1. Open a new document in Microsoft Word
2. Go to File and select Save As
3. In the File Name field, type a descriptive name for your file
4. In the drop-down menu below the file name, ensure the Save As type field is set to "Word Document (.docx)"
5. Press the Enter key or click the Save button

**How to Check Your Work:**
1. Look at the title bar at the very top of the Microsoft Word application window
2. Check that the file name is descriptive and identifies the document or its purpose
3. Ensure the document is saved in the .docx file format

**Inaccessible Examples:**
- Document1.doc - not descriptive AND not in .docx format
- application.docx - not descriptive
- Yesterday's list.docm - macro-enabled file, cannot be tested with these instructions

**Accessible Examples:**
- OMB Report 387 2016 v2.docx
- cell phone request application 2016.docx
- AEDCOP attendance 2016.docx

---

### DOCX Module 2: Use Styles to Create Headings

**Description:** Learn how to add headings and formatting so assistive technology can infer meaning from the document structure using built-in styles.

**Key Concept:** "Assistive technology cannot infer meaning from formatting characteristics alone, such as bolded or underlined text." Using built-in styles creates structure that AT can access and interpret.

**How to Author Accessibly:**
1. Click on the Home tab in the navigation ribbon
2. Go to the Styles section and choose one of the available styles from the gallery
3. Use styles such as Heading 1, Heading 2, Heading 3, etc. when creating headings
4. Method A: Select a style from the gallery, then start typing
5. Method B: Type your heading first, then select a style from the gallery
6. If document contains multiple heading levels (Major, Section, Subheading, etc.) and each level is visually different, use a separate style for each heading level
7. You can modify the visual characteristics of any style for desired look and feel

**How to Check Your Work:**
1. Open the navigation pane (View tab > check Navigation Pane box, OR Ctrl+F)
2. Ensure the "Headings" tab is selected
3. Verify ALL headings appear in the navigation pane
4. Verify heading structure matches the visual outline of the document

**Inaccessible Example:** Text formatted to LOOK like headings (bold, larger) but none appear in navigation pane - structural information for AT is not present.

**Accessible Example:** Same visual appearance but headings are displayed in the navigation pane and structure matches visual outline.

---

### DOCX Module 3: Use Built-in Features to Create Lists

**Description:** Use lists to organize and structure content so assistive technology can identify that information is in a group and convey relationships between items.

**Key Concept:** "Assistive technology cannot infer meaning from visual formatting alone." Using Microsoft's built-in list features creates the structure AT can identify and interpret.

**How to Author Accessibly:**
1. Go to the Home tab in the navigation ribbon
2. In the Paragraph section, use the Bullets, Numbering, or Multi-Level List feature
3. Method A: Select list option from ribbon, then begin typing
4. Method B: Type the first item, then click the list button from ribbon

**How to Check Your Work:**
1. Place cursor after the text of one of your list items
2. Ensure one of the list options from the navigation ribbon is selected/highlighted
3. Alternative: Open the Reveal Formatting pane (Shift+F1)
4. Ensure "Bullets and Numbering" category appears in the formatting pane

**Inaccessible Indicators:**
- No list buttons highlighted in navigation ribbon when text selected
- "Bullets and Numbering" category absent from Reveal Formatting pane
- Lists created using dashes, asterisks, or tabs without built-in features

**Tools Referenced:** Bullets, Numbering, Multi-Level List features; Reveal Formatting pane (Shift+F1)

---

### DOCX Module 4: Use Built-in Features to Organize Content

**Description:** When laying out a document, learn how to use the built-in columns tool to ensure content is read in the proper reading order.

**Key Concept:** "Screen readers and other assistive technology cannot present information in the correct reading order if only tabs or spaces are used to create the appearance of columns." Reading order follows top to bottom, then left to right.

**How to Author Accessibly:**
1. Type your content
2. Select the content you want placed into columns
3. Navigate to the Page Layout tab in the ribbon
4. Select the Columns button
5. Choose desired number of columns from the drop-down list

**How to Check Your Work:**
1. Place cursor in text believed to be in columns
2. Open Reveal Formatting pane using Shift+F1
3. Verify "columns" is listed under Section category
4. May need to expand Section by selecting button next to it

**Inaccessible Example:** Uses tabs or spaces for visual column appearance; Reveal Formatting pane shows no columns indication; paragraph marks reveal arrow formatting marks indicating tab usage.

**Accessible Example:** Formatting pane shows "columns, number of columns, two"; content correctly structured.

**Tools:** Page Layout tab, Columns button, Reveal Formatting pane (Shift+F1), Show Paragraph Marks feature

---

### DOCX Module 5: Use Built-in Features to Create Layout and Data Tables

**Description:** Learn how to use built-in features to create layout tables and both simple and complex data tables so assistive technology can read information meaningfully.

**Three Table Types:**
1. **Layout Tables** - uses cells to layout/format images or text
2. **Simple Data Tables** - require row/column header information for cell understanding
3. **Complex Data Tables** - have multi-level headings or merged/split cells

**Accessibility Rules:**

*Layout Tables:*
- Reading order must match visual layout
- Table must be placed in line

*Simple Data Tables:*
- Must be inserted as actual table, not image
- Keep simple: one header row, no merged/split cells
- Must identify header row
- Must be placed in line with text

*Complex Data Tables:*
- CANNOT be made accessible in Word
- Must transfer to another application

**How to Author - Layout Tables:**
1. Click Insert tab > Tables section > Table > Insert Table
2. Enter column and row numbers
3. Fill with content ensuring left-to-right, top-to-bottom reading order
4. Verify table is in line via Table Properties

**How to Author - Simple Data Tables:**
1. Insert table with Insert > Table > Insert Table
2. Add data with headers in first row
3. Select first row
4. Go to Layout tab > Table Tools > Data section > Click "Repeat Header Row"
5. Verify table properties set to "None" for text wrapping

**How to Check - Layout Tables:**
1. Place cursor in first cell
2. Press Tab key to navigate; verify order matches visual layout
3. Right-click table > Table Properties > Table tab > Verify "None" in Text Wrapping section

**How to Check - Simple Data Tables:**
1. Click table; verify Table Tools tab appears (NOT Picture Tools)
2. Turn on grid lines: Layout tab > View Grid Lines button
3. Check for merged/split cells
4. Place cursor in header row, press Shift+F1 for Reveal Formatting pane
5. Verify "Repeat as Header Row" is identified
6. Right-click > Table Properties > Verify text wrapping set to "None"

**Inaccessible Indicators:**
- Picture Tools tab appearing (indicates image of table, not actual table)
- Tab order reading column-by-column instead of matching visual layout
- Text wrapping set to "Around" instead of "None"
- Merged cells present in simple data tables
- Header row not identified as repeat header row

---

### DOCX Module 6: Identify Distinct Languages

**Description:** Use proofing language settings to programmatically identify document languages, enabling assistive technology to read and correctly pronounce content in multiple languages.

**Key Concept:** "Since screen readers cannot infer meaning from just text alone, it is important to identify these distinct languages."

**Exceptions:** You do NOT have to set a language for proper names, technical terms, or foreign words that are part of common English usage.

**How to Author Accessibly:**
1. Select text written in a different language
2. Navigate to Review tab
3. Click Language button
4. Choose "Set Proofing Language" from submenu
5. Select appropriate language from dialog box
6. Click OK

**How to Check Your Work:**
1. Place cursor in text section with different language
2. Go to Review tab
3. Click Language button
4. Select "Set Proofing Language"
5. Verify correct language is selected

---

### DOCX Module 7: Create Unambiguous Names for Links

**Description:** Learn how to add links so each link has a unique and descriptive name allowing assistive technology users to determine the destination, function, or purpose.

**Key Concept:** "If you have several links on your page, all labeled as 'Click Here,' then assistive technology will not be able to convey to individuals with disabilities information that distinguishes one link from another."

**Two Approaches:**
1. Ensure destination/purpose/function of each link is described in surrounding text
2. Create a hyperlink using the descriptive text itself

**How to Author - Creating Hyperlinks:**
1. Type descriptive text describing link destination or purpose
2. Select the text for hyperlinking
3. Access Insert tab > Navigation Ribbon > "Hyperlinks" button (Links section)
4. For external links: type accurate URL in address field, click OK
5. For internal links: click "Place in this document," select appropriate location
6. For email links: click "Email Address" button, type address, select OK

**How to Check Your Work:**
- Verify each link can be determined within context surrounding the link
- OR has an unambiguous name describing destination, function, or purpose

**Inaccessible Example:** Two links both labeled "Click Here" - one for New Software, one for Old Software. User cannot distinguish between them.

**Accessible Example:** "New Software" and "Old Software" text portions are directly hyperlinked.

---

### DOCX Module 8: Duplicate Vital Information in Headers, Footers and Watermarks

**Description:** Screen readers do NOT automatically read information in headers, footers, and watermarks.

**Core Requirement:** "When vital information such as response date, security levels, or distribution instructions are placed in these areas, that same information must be duplicated at or near the start of the main content area."

**How to Author:** Duplicate any critical data from headers, footers, or watermarks at or near the beginning of relevant sections or the document start.

**How to Check Your Work:**
1. Examine the document for vital information in headers, footers, or watermarks
2. Navigate to the content area start
3. Verify the vital information has been duplicated in a secondary location

**Inaccessible Example:** "Draft document" appears only in header.
**Accessible Example:** "Draft document" appears in both header and body content.

---

### DOCX Module 9: Create Accessible Images and Other Objects

**Description:** Assistive technology cannot infer meaning from images and other objects such as pictures, images of text, images of tables, shapes, and icons with hyperlinks.

**Key Requirements:**
1. Place images and objects inline with text for proper reading order
2. Add descriptive text to images and objects

**Three Methods for Adding Descriptive Text:**
- Alternative text (alt text)
- Captions
- Information in surrounding text or appendix

**Guidelines for Descriptive Text:**
- Describe purpose and/or function of meaningful objects
- For decorative/non-meaningful images: use a space or `" "` as descriptive text
- For images containing text: alt text must match text verbatim
- Keep descriptions brief - approximately 250 characters or less

**How to Position Image Inline:**
1. Click the image or object
2. Navigate to Page Layout tab
3. In Arrange section, select Position
4. Choose "in line with text"

**How to Add Alt Text:**
1. Click the image or object
2. Right-click and select format option (Format Picture, Format Object, or Format Chart)
3. Click Layout and Properties icon
4. Select Alt Text
5. Enter description in the "description" field
6. Click Close button
(Note: Word 2010 users select Alt Text directly after Format Picture)

**How to Check Your Work:**
1. Verify all images and objects are positioned inline with text
2. Confirm descriptive text exists as alt text, caption, or surrounding text
3. Run Microsoft Accessibility Checker: File > Info > Check for Issues > Check Accessibility
4. Verify no errors appear for "object not in line"
5. Right-click each image and verify Alt Text section contains appropriate description

**Inaccessible Examples:**
- Using filename as alt text ("logo.jpg") instead of describing what the logo represents
- Detailed description of decorative balloons should instead be empty: `" "`
- Image not positioned inline with text, flagged by accessibility checker

**Accessible Example:** Logo alt text: "Accessible electronic document, Community of Practice, AEDCOP logo"

---

### DOCX Module 10: Create Accessible Textboxes

**Description:** Screen readers and other assistive technology cannot access information in text boxes unless they are placed "in line with text."

**How to Author Accessibly:**
1. Click on a textbox in the document
2. Navigate to the Page Layout tab in the ribbon
3. Locate the Arrange section
4. Select Position
5. Choose "in line with text" option

**How to Check Your Work:**
1. Open the File menu
2. Select Info
3. Click "Check for Issues"
4. Select "Check Accessibility"
5. Review results in the right-side window
6. Verify no errors appear for "Object Not In Line"

**Tool:** Microsoft Accessibility Checker

---

### DOCX Module 11: Use Color and Other Sensory Characteristics Plus Text to Convey Meaning

**Description:** Color and other sensory characteristics (size, shape, position) should NOT be the only way to convey meaning. Individuals who are blind, have low vision, or are colorblind cannot access information conveyed solely through visual means.

**How to Author:**
- Use color and sensory characteristics to convey meaning
- Include accompanying text that duplicates the meaning
- Combine visual and textual information

**How to Check Your Work:**
- Locate instances where color or characteristics convey information
- Verify text that replicates that meaning exists
- Test accessibility by removing color

**Inaccessible Example:** Project status table using only colored boxes (green, yellow, red) without text labels.
**Accessible Example:** Same table with text inside boxes ("Completed," "At risk," "Incomplete") alongside colors.

---

### DOCX Module 12: Create the Required Color Contrast

**Description:** Ensure enough color contrast between foreground and background when selecting color palettes.

**WCAG Contrast Requirements:**
- **Standard Text:** Contrast ratio >= 4.5:1
- **Large Text (14pt bold or 18pt regular):** Contrast ratio >= 3:1
- **Exceptions:** Text that is incidental, overlaid on images, or logotype is excluded

**Tool:** Color Contrast Analyzer - downloadable from www.pacielogroup.com/resources/contrast-analyzer (can be run without installation)

**How to Test:**
1. Open the Contrast Analyzer application
2. Select the eyedropper button in the foreground section
3. Place the crosshair on a text pixel and click to sample color
4. Select the eyedropper button in the background section
5. Place the crosshair on a background pixel and click
6. Check results in "luminosity section" at bottom of window
7. Compare ratio against requirements; adjust colors if needed and retest

**Example Contrast Ratios:**
- 1.41:1 (gold/yellow) - NOT accessible
- 3.72:1 (red/yellow) - NOT accessible
- 4.5:1 (green/dark green) - minimum acceptable
- 5.15:1 (blue/white) - accessible
- 21:1 (black/white) - accessible

---

### DOCX Module 13: Create Accessible Embedded Files

**Description:** When embedding audio-only, video-only, or multimedia files with meaningful information, authors must provide additional content for comparable access.

**Requirements by Media Type:**

| Media Type | Requirement |
|------------|-------------|
| Audio-only | Include an accurate and complete transcript |
| Video-only | Include an accurate and complete text description |
| Multimedia (audio+video) | Provide accurate and complete synchronized captioning AND audio descriptions |

**Definitions:**
- **Transcript:** a text version of exactly what is being said in the audio file
- **Text description:** a text version of what is being shown in a video-only file
- **Captions:** time-synchronized text version of what is being said and/or description of relevant sounds
- **Audio descriptions:** time-synchronized descriptions of what is being shown

**How to Check:** Activate the embedded file and compare corresponding text for accuracy and completeness.

---

## PART 2: PDF DOCUMENTS (PDF Series)

### Series Overview
- **Series Title:** "How to Test and Remediate PDFs for Accessibility Using Adobe Acrobat DC"
- **Total Duration:** 63 minutes
- **Parts:** 5-Part Video Series (Modules 0-4)
- **Created By:** AED-COP, Chief Information Officers Council, and Federal Aviation Administration
- **Based On:** Section 508 Baseline Test Guide for PDFs and Section 508 PDF Checklist
- **Policy Note:** "Federal policy requires agencies to prioritize HTML and use PDFs only when necessary."

**Note:** The /training/pdfs/ index page returned 404, but all individual module pages were accessible.

---

### PDF Module 0: Introduction & Background

**Description:** Introduces AED-COP and foundational concepts for making PDFs Section 508 Conformant.

**Background:**
- AED-COP established in October 2012 by federal agency subject matter experts
- Video series assists in testing and remediating PDFs for Section 508 Conformance
- "All information and computer technology must be accessible to persons with disabilities" (Section 508 requirement)
- "The majority of PDFs created do not comply with Section 508 of the Rehabilitation Act"

**Learning Objectives - by series completion, learners will understand:**
1. What is a PDF?
2. How to test a PDF for accessibility
3. How to remediate a PDF for accessibility

**Components Covered:**
- Content layer examination
- Tag layer analysis
- Logical reading order evaluation
- Adobe Acrobat accessibility checker utilization

---

### PDF Module 1: What is a PDF?

**Description:** Learn about PDF elements, tag types, accessibility checklists, document conversion methods, and PDF testing procedures.

**PDF Structure - Three Layers:**
1. **Physical View:** Visual representation of text and graphics (print view)
2. **Content View:** Displays textual and graphical information on the page
3. **Tag Structure Tree:** Establishes logical document structure and reading order for assistive technology

**Accessing PDF Layers in Adobe Acrobat DC:**
- Content Layer: View menu > Show/Hide > Navigation Pane > Content
- Tags Layer: View menu > Show/Hide > Navigation Pane > Tags

**Common PDF Tag Types:**
- Paragraph, Heading, List, Figure, Table, Table Row, Table Header Cell, Table Data Cell, Artifact
- PDFs can use up to 37 different tags total
- Tags from source files may display different names but are valid if mapped to standard Acrobat tags

**Viewing Full Tag Names:** Right-click tag > Properties > check "Tags Type" field
**Expanding All Tags:** Hold Shift + press 8 key

**Accessibility Checklist Location:** www.section508.gov/refresh-toolkit/test
- All conditions must be "Yes" or "Not Applicable"
- Any "No" must be resolved before document is accessible

**Document Conversion Method A - Using Adobe Acrobat DC (non-Microsoft Office docs):**
1. Open Acrobat
2. File menu > Create > PDF from File
3. Locate and select file
4. Select Open
- Note: May generate tagged or untagged PDFs; untagged are NOT accessible

**For Scanned Documents:**
- Must perform OCR first
- Indicators: cannot highlight/select text, blurry or handwritten text

**Document Conversion Method B - From Microsoft Office:**
Prerequisite: Verify Office document is as accessible as possible first
1. Open desired Office file
2. Office Applications menu bar > Acrobat > Preferences
3. Set conversion settings:
   - View Adobe PDF results: checked
   - Prompt for PDF file name: checked
   - Convert document information: checked
   - PDF A Compliance: set to None
   - Create bookmarks: checked
   - Add links: checked
   - Enable accessibility and reflow with tagged Adobe PDF: checked
4. Advanced Settings > Change Compatibility to "Acrobat 8.0 PDF 1.7"
5. Select Acrobat > Create PDF from Office Applications menu
6. Name file and save
- Expected: Documents using proper formatting typically generate 90% accessible PDFs

**PDF Testing Tool:** Adobe's Accessibility Full Check: Tools > Accessibility > Full Check

---

### PDF Module 2: Testing a PDF for Accessibility

**Description:** Covers steps for testing PDF accessibility, including document property setup, manual content evaluation, automated checks, and text-to-speech tools.

**13 Testing Areas:**

#### 1. Document Properties Setup
**Required elements to verify:**
- Descriptive file name and tags present
- Title field contains descriptive title
- Initial View set to show Document Title
- Tagged PDF option set to "Yes"
- Content Copying for Accessibility set to "Allowed" (Security tab)
- Primary language properly assigned
- **Critical:** "If the Title field is missing a descriptive title and Document Title is not selected, the PDF is considered not accessible."

#### 2. Content Verification
**Visual inspection for:**
- Proper heading level structures
- Descriptive alternative text on images/objects
- Clear document layout and formatting
- Scanned document detection (blurry/handwritten = needs OCR)

**Steps:**
1. Go to Acrobat's View menu > Show Hide > Navigation Panes > Content
2. Expand content tree (Shift + 8)
3. Use arrow keys to navigate
4. Verify physical view highlights as you navigate

#### 3. Tag Structure Examination
1. View menu > Show Hide > Navigation Panes > Tags
2. Expand tags tree (Shift + 8)
3. Navigate using arrow keys
4. Verify tags match physical document elements
- **Key:** "All elements in a PDF must be tagged. If the element is not tagged, it will not be accessible by assistive technology."
- Finding tag type: Right-click on tag > Properties > Type field

#### 4. Logical Reading Order Verification
1. Open tags pane and expand tag structure tree
2. Use up/down arrow keys to navigate
3. Confirm tags follow visible logical layout
- **Critical:** "Tags must follow the visible logical layout of the page."

#### 5. Tab Order Evaluation (for PDFs with links/form fields)
1. Press Tab key to navigate
2. Verify keyboard focus follows visible logical layout
3. Confirm proper sequence

#### 6. Figure/Image Assessment
1. Tools > Accessibility > Reading Order > Check, Show Tables and Figures
2. Look for text descriptions on meaningful images
3. Right-click figures > Edit Alternate Text
4. Examine captions and surrounding content
5. For images containing text, confirm description matches text verbatim
- **Critical:** "Without alternative text, users unable to see images will not be able to access all information."

#### 7. Data Table Evaluation - Simple Tables
1. Identify data table in document
2. Go to Tags pane
3. Click Selection tool and select first data cell
4. Tags pane > Options > Find Tag from Selection
5. Expand Table Tags
6. Verify header cells tagged as Table Header (TH)
7. Verify non-header cells tagged as Table Data (TD)
- Images of data tables are NOT considered accessible

#### 7b. Data Table Evaluation - Complex Tables
1. Open Order pane > Options > Show Reading Order Panel
2. Select Reading Order number for data table
3. Reading Order panel > Table Editor
4. Right-click each header > Table Cell Properties
5. Verify scope set to "column header" or "row header"
6. For spanning cells, verify span identifies proper number
- **Note:** "The Table Editor tool may not always function properly. Use tag structure tree and assistive technology to verify."

#### 8. Form Field Elements
1. Press Tab to find form elements
2. Hover over each form field to reveal tooltip
3. Verify tooltips match label/instructions
4. Check tab order matches visual and logical order
- **Note:** If PDF producer is Adobe LiveCycle Designer, these steps are insufficient
- Check PDF producer: File menu > Properties > PDF Producer field

#### 9. Link Evaluation
1. Press Tab to find links
2. Check each link has unambiguous name
3. If image is link, verify alt text states link purpose
4. Confirm tab order matches visual and logical order
- "If the link is not unique...the PDF is not accessible."

#### 10. Sensory Characteristics
1. Find color and sensory characteristics
2. Verify text conveys the same meaning
- "Without text, individuals who are blind, low vision, or color blind will not have access to comparable information."

#### 11. Color Contrast Assessment
- Standard-sized text: 4.5:1 ratio
- Large text (14pt bold or 18pt regular): 3:1 ratio
- Incidental/logotype text excluded

**Using Color Contrast Analyzer:**
1. Download Color Contrast Analyzer
2. Drag foreground eyedropper over text sample
3. Drag background eyedropper over background color sample
4. Verify contrast ratio passes Level AA

#### 12. Acrobat Accessibility Full Check
1. Tools menu > Accessibility > Full Check
2. Select "Select All" button
3. Select "Start Checking"
4. Review report (red circle X = errors)
5. Right-click error > Fix (if available)
6. If no Fix option: select "Show in Tags pane" > right-click tag > Properties > change Type
7. Right-click repaired error > "Check Again"

#### 13. Read Out Loud Text-to-Speech Tool
1. View menu > Read Out Loud > Activate
2. View > Read Out Loud > Start
- **IMPORTANT:** "The Read Out Loud screen reader does not function the same as a dedicated screen reader such as JAWS. Therefore, it must NOT be used for testing Section 508 conformance."

**Prerequisites:**
- Visual verification in Adobe Acrobat requires ability to view screen and use mouse
- If PDF contains attachments or is a portfolio, each individual document must be evaluated separately

**Tools Referenced:** Adobe Acrobat DC, Color Contrast Analyzer, Acrobat's Full Check, Read Out Loud, JAWS

---

### PDF Module 3: Remediating PDFs for Accessibility

**Description:** How to fix document properties, add/adjust tags, adjust reading and tab order, add alt text, and set language properties.

#### Document Properties Remediation
**Three Requirements:**
1. Descriptive filename
2. Allow copying content for accessibility
3. Specified primary language

**Steps:**
1. File > Properties > Description > Add descriptive title
2. Initial View tab > verify "document title" in Show dropdown
3. Security tab > change to "Allow Content Copying for Accessibility" if restricted
4. Advanced tab > choose correct Language from dropdown
- "Setting the proper document language enables screen readers to choose the correct synthesizer."

#### Autotagging Process
1. View > Navigation Panels > Tags
2. Tags pane: Options > Add Tags to Document
3. Examine and manually correct improper tags

#### Converting Improper Tags
1. Open Tag Panel
2. Click desired tag > Properties
3. From Type combo box, select correct tag
4. Close window

#### Removing and Rebuilding Tag Structure
When multiple elements improperly tagged:
1. Tags panel > select root tag "Tags" > Delete (removes all)
2. Order pane: View > Navigation Panels > Order
3. Options > Clear Page Structure > Yes
4. Verify page structure cleared
5. Order pane > Options > Show Reading Order Panel
6. Draw container around each element needing tags
7. Tag headers, figures, tables, form fields, and paragraphs individually

#### Touch-Up Reading Order Tool Tags Reference

| Button | Purpose | Tag Generated |
|--------|---------|---------------|
| Text | Paragraphs and lists | P tag |
| Form Field | Form field elements | Form tag |
| Heading 1 | Document title | H1 tag |
| Heading 2 | Subheadings | H2 tag |
| Heading 3 | Subheadings | H3 tag |
| Figure | Images and objects | Figure tag |
| Figure Captions | Figures with captions | Captions tag |
| Table | Data tables | Table, TR, TH, TD tags |
| Cell | Table data cells | TD tag |
| Formula | Equations | Formula tag |
| Background | Decorative/artifact images | Background tag |

#### Create Tag from Selection (for block quotes, notes, references)
1. Open Touch-Up Reading Order tool
2. Navigate to Tags pane
3. Draw container around content
4. Tags pane Options > Create Tag from Selection
5. Select appropriate tag type > OK

#### New Tag Tool (for complex content like lists)
1. Tags pane > select tag location > Options > New Tag
2. Select desired tag (e.g., List)
3. Draw container around each list item via Touch Up Reading Order
4. Create Tag from Selection for each
5. Select all list item tags > drag below list tag

#### Manually Tagging Links
1. Select text > tag as "Text" from Touch Up Reading Order
2. Right-click > Create link OR Tools > Link
3. Draw selection box > create link
4. Tags pane > Find Tag from Selection
5. Change P tag to Link
6. Nest Link tag inside P tag
7. Find > select Unmarked Links or Unmarked Annotations
8. Check Search Page > Find Next > Tag Element
- **Important:** "Do not use Search Document to tag individual missing links. Select the linked text for each annotation."

#### Remediating Data Tables
**Two possible errors:**
- Table Header error: lacks Table Header tags
- Table Regularity error: content not belonging in structure OR Scope/Span not set

**Steps:**
1. Ensure caption/title not tagged as part of table
2. Order pane > Options > Show Reading Order Panel
3. Select Reading Order number for table
4. Reading Order Panel > Table Editor
5. Right-click each header cell > Cell Properties
6. Set column headers as Table Header with Scope = Column Header
7. Set row headers with Scope = Row Header
8. For spanning cells, set Span to proper number

#### Form Field Remediation (Adobe Acrobat Pro only)
**Adding Tooltips:**
1. Tools > Prepare Form
2. Select Form Field > Right-click > Properties
3. General tab: add tooltip

**Adjusting Tab Order:**
1. Form Edit Mode > More > Show Tab Numbers
2. View Form Fields list
3. Drag and drop into correct order

#### Deleting Empty Tags
1. Open Tags pane > navigate tag structure
2. Find tag with no child element
3. Select > Delete (Ctrl+Z to undo if needed)

#### Adding Alternative Text
1. Select tag in tags tree > Properties
2. Go to Alternative Text field
3. Add appropriate text > Close > Save

#### Setting Language for Individual Tags
1. Tags Tree > select tag with different language
2. Right-click > Properties
3. Language dropdown > select appropriate language
- "If desired language not in list, a language pack may need to be purchased from Adobe."

#### Adjusting Logical Reading Order
1. View > Navigation Panel > Order
2. Numbers appear indicating rearrangeable elements
3. Click box left of element > drag to proper location
- "Tags in the Tags Tree will rearrange to match the order set by the order pane."

#### Setting Logical Tab Order
1. Open Page Thumbnails pane
2. Select first thumbnail > Ctrl+A (select all)
3. Options > Page Properties
4. Tab Order: select "Use Document Structure"

---

### PDF Module 4: Converting Scanned Documents into Section 508 Conformant PDFs

**Description:** Learn to identify scanned pages, perform OCR, correct text, enhance pages, and handle signed memorandums.

**Identifying Scanned Pages:**
- Blurry pages or handwritten information
- Cannot highlight or select text
- View > Show Hide > Navigation Panes > Content > expand tree

**Adobe DPI Recommendations:** Grayscale = 300 DPI, Color = 600 DPI

**OCR Suspects:** "Renderable text or images that may not have been recognized properly by the software."

**Performing OCR:**
1. Tools > Enhance Scans > Recognize Text > In this file
2. Settings: identify pages, set language, output to searchable images, set DPI
3. Select OK > Recognize Text

**Correcting OCR Suspects:**
1. Recognize Text toolbar > Correct Recognized Text
2. Review each boxed suspect
3. Accept if correct, or type correction
4. Repeat for all suspects

**Enhancing Scanned Pages:**
1. Enhance Scans toolbar > Enhance > Scanned Document
2. Settings > configure > OK > Enhance

**Evaluating OCR Results:**
1. Tools > Export PDF > Word Document > Export
2. Compare exported content to original PDF

**Editing Textual Content:**
1. Tools > Edit PDF > use edit tools > close when done

**Correcting via Tags Properties:**
1. Open Tags pane > Selection tool > select OCR error
2. Tags pane Options > Find Tag from Selection
3. Right-click tag > Properties > add corrected text to "actual text" field

**Handling Signed Memorandums:**
1. Print document for signature
2. Scan signed pages to PDF
3. Perform OCR and markup on scanned pages
4. Verify Word document before conversion
5. Merge: Tools > Organize Pages
6. Right-click > Delete pages if needed
7. Insert > From File > choose scanned PDF
8. Drag to correct location
9. Save with descriptive title
10. Optional: File > Save as Other > Reduce to Size PDF

---

## PART 3: MICROSOFT EXCEL SPREADSHEETS (XLSX Series)

### Series Overview
- **Series Title:** "How to Make an Accessible Spreadsheet in Microsoft Excel"
- **Total Duration:** 30 minutes 50 seconds
- **Parts:** 12-Part Video Series (Modules 0-10)
- **Created By:** AED-COP, Chief Information Officers Council, and Federal Aviation Administration
- **Based On:** Section 508 Baseline Test Guide for Excel and Section 508 Excel Checklist
- **Scope Limitations:** Does NOT cover worksheets with macros (.xlsm), programmed formatting, or forms-enabled/restricted documents

**Note:** The /training/spreadsheets/ index page returned 404.

---

### XLSX Module 0: Introduction & Background

**Description:** Introductory module explaining minimum steps for Section 508 conformant Excel worksheets.

**Key Topics Covered:**
- Document formatting
- Text formatting
- Object formatting
- Color formatting
- Audio, video and synchronized media

**Module Structure:** Each module contains: topic introduction, how to author for accessibility, how to test for accessibility, inaccessible examples, accessible examples.

**Context:** Persons with disabilities use "screen readers or text-to-speech software" to access electronic information.

---

### XLSX Module 1: Using Built-in Features to Organize Content and Ensure Logical Reading Order

**Description:** Use cell styles, heading levels, and data table formats to ensure logical reading order.

**Key Principle:** "The logical reading order for an Excel spreadsheet is always left to right and top to bottom," beginning at cell A1.

**Recommended Features:**
1. **Worksheet Naming** - Apply descriptive and unique names to spreadsheet tabs
2. **Heading Levels** - Use cell style tool for title and heading levels 1-9
3. **Table Formatting** - Use "format as table" tool for data tables
4. **Cell Formatting** - Apply formatting tool for cells requiring special formatting

**Best Practices Checklist:**
- Start all worksheets at cell A1
- Avoid spanning content across multiple rows/columns
- Ensure visual/logical reading order flows left-to-right, top-to-bottom
- Verify sheet navigation using arrow keys matches logical reading order
- Content spanning multiple rows/columns creates accessibility failures

---

### XLSX Module 2: Ensuring the Contrast Ratio Between Text and Background is Sufficient

**WCAG Contrast Standards:**
- Standard text (12pt regular): 4.5:1 minimum
- Large text (14pt bold or 18pt regular): 3:1 minimum
- Excluded: incidental text, text on images, logos
- Black text on white backgrounds need not be tested

**Tool:** Color Contrast Analyzer (www.pacielogroup.com/resources/contrastanalyzer)

**Steps:**
1. Download/open Color Contrast Analyzer
2. Drag foreground eyedropper over text sample
3. Drag background eyedropper over background sample
4. Verify ratio meets threshold
5. Adjust colors if failing

**Sufficient Examples:** White on black, dark green on yellow, light blue on dark blue, white on red
**Insufficient Examples:** Dark gray on black, orange on yellow, red on blue, dark green on red

---

### XLSX Module 3: Ensuring Color and Other Visual Characteristics are Also Described in Text

**Key Concepts:**
- Color alone cannot be the only method to convey information
- Visual characteristics requiring text: color, size, shape, and location
- Users who are blind, have low vision, or are colorblind need equal access

**Testing:** Identify where color/visual characteristics convey meaning, verify text duplicates that meaning.

**Inaccessible:** Status shown only through color (green, yellow, red) without text
**Accessible:** Color-coding paired with explicit text descriptions

---

### XLSX Module 4: Making Vital Background Information Accessible

**Key Concept:** Assistive technology cannot automatically read headers, footers, and watermarks.

**Requirement:** "Any vital information must be duplicated in cell A1."

**Examples of Vital Information:** "Respond by X date," "Confidential," "Do not distribute"

**Critical Limitation:** "Watermarks in Excel are floating objects and cannot be made accessible" - duplication in cell A1 is the required workaround.

---

### XLSX Module 5: Using Built-In Features to Create Data Tables

**Key Inaccessibility Issues:**
- Pictures of tables are not accessible
- Tables with merged or split cells are not accessible
- Data tables inside other tables create barriers
- Image-based tables never appear in navigation tools

**How to Create Accessible Data Tables:**
1. Select Insert > Table
2. Choose cell range in Create Table pane
3. Check "My Table has headers" checkbox
4. Name the table: Table Tools > Design > Table Name
5. Update column/row headings with descriptive names
6. Verify table name displays under Table Tools Design Properties

**Testing Steps:**
1. Use Home tab > Editing > Find and Select > Go To to locate tables
2. Confirm table names display in Table Tools Design menu
3. Verify "Header Row" and/or "First Column" checked in Table Styles Options
4. Confirm ribbon shows Table Tools tab (NOT Picture Tools)

---

### XLSX Module 6: Adding Alternative Text to Images and Other Objects

**Key Concept:** "Assistive technology cannot infer meaning from images and other objects."

**Alt Text Guidance:** "Think about the purpose of the image and not what the image looks like."

**Quality Test:** "If you removed the image and replaced it with alternative text and no key information was lost, you provided proper descriptions."

**Critical Excel Limitation:** "In Excel, images, objects, shapes, charts, and other non-text elements cannot be anchored or embedded in a cell."

**Workaround Solutions:**
- Add descriptive text in nearby cells adjacent to the object
- Create a separate appendix listing all non-text elements with descriptions

---

### XLSX Module 7: Creating Links with Unique and Descriptive Names

**Requirements:**
- Each link must have a unique and descriptive name
- Link purpose should be discernible from surrounding content
- Generic "Click Here" links are problematic

**Steps to Create Accessible Links:**
1. Copy URL, select desired text, right-click (or Shift+F10), select Link
2. Enter URL in address field > OK
3. To edit: right-click link > Edit Hyperlink
4. Click "Text to Display" field > enter descriptive text

**Testing:** Verify all hyperlinks have meaningful names describing destination/function/purpose.

---

### XLSX Module 8: Ensuring Descriptions of Embedded Audio, Video and Multimedia Files are Accurate

**Requirements:**
- Audio-only: accurate and complete transcript required
- Video-only: accurate and complete text description required
- Multimedia: accurate and complete synchronized captions AND audio descriptions required

**Testing:** Activate the media file. Verify accurate and complete description/transcript/captions exist.

**Note:** "If the document does not contain audio, video, or multimedia files, you do not need to perform this test."

---

### XLSX Module 9: Excluding Flashing Objects

**Rule:** "Flashing objects can cause seizures and should never be used. An Excel Worksheet that includes flashing objects cannot be considered accessible."

**Test:** "Is the document free of all flashing objects?"

---

### XLSX Module 10: Saving in the .xlsx Format with a Descriptive Filename

**Requirements:**
1. "Is the file name descriptive, and does it identify the document or its purpose?"
2. "Is the file saved as an Excel workbook in the .xlsx format?"

**Steps:**
1. Open new document in Excel
2. File > Save As
3. Save as Excel workbook (.xlsx)
4. Use descriptive filename
5. Verify in Windows Explorer or title bar
6. Enable File Name Extensions in View if needed

**Fails:** Spreadsheet1.xls (non-descriptive, wrong format)
**Passes:** FundingBudget.xlsx (descriptive, correct format)

---

## PART 4: MICROSOFT POWERPOINT PRESENTATIONS (PPTX Series)

### Series Overview
- **Series Title:** "How to Author and Test Microsoft PowerPoint Presentations for Accessibility"
- **Total Duration:** 43 minutes 54 seconds
- **Parts:** 14-Part Video Tutorial (Modules 0-13)
- **Created By:** AED-COP, Chief Information Officers Council, and Federal Aviation Administration
- **Scope Limitations:** Does NOT cover PPTM (macro-enabled) files or restricted documents

**Additional Training:** "Creating PowerPoint Templates" (8m 18s, 1-Part) - covers using Slide Master for accessible templates.

**Note:** The /training/presentations/ index page returned 404.

---

### PPTX Module 0: Introduction & Background

**Description:** Explains minimum steps for Section 508 conformant PowerPoint presentations.

**Key Topics:**
- Document formatting
- Text formatting
- Object formatting
- Color formatting
- Audio, video, and synchronized media

**Purpose:** Ensure persons with disabilities (blind, low vision, deaf, hard of hearing, physical/cognitive disabilities) receive equal access.

**AT Context:** Screen readers and text-to-speech software "assist individuals with disabilities by reading out loud visual and non-visual electronic content."

---

### PPTX Module 1: Creating the Presentation's Layout Design and Establishing the Logical Reading Order

**Description:** Use slide layouts, themes, and customized master slides to establish logical reading order.

**Best Practices:**
1. **Simple/Clean Layout** - "defines the content structure but also establishes the logical reading order for assistive technology"
2. **Background Design** - Avoid colored/patterned backgrounds that make content hard to read
3. **Font Selection** - "avoid using script style fonts" (e.g., Blackadder Italic, Brush Script) - cause eye fatigue and challenge cognitive/visual impairments
4. All checklist conditions must receive "yes" responses

**Adding/Modifying Slides:**
- Home or Insert tab > new slide icon > dropdown for specific layouts
- Change layout: Select slide > Home tab > Layout > choose layout
- Slide Master: View > Slide Master for custom layouts

**Establishing Reading Order:**
- Default: Screen reader reads slide title first, then other content in layout order, then additional content in order added
- **Selection Pane:** Home > Drawing > Arrange > Selection Pane
- Reading order is bottom to top in the list
- Click and drag objects to reorder, or use arrow buttons
- Arrange dropdown: Bring to Front (read last), Send to Back (read first), Bring Forward, Send Backward
- Eye icon: hide visually while maintaining screen reader access

**Testing:** "Starting from the bottom and moving to the top, select each object to view the reading order on the slide. Does the selection of each object match the visual reading order?"

---

### PPTX Module 2: Ensuring the Contrast Ratio Between Text and Background is Sufficient

**WCAG Standards:**
- Standard Text (12pt regular): 4.5:1 minimum
- Large Text (14pt bold or 18pt regular): 3:1 minimum
- Excluded: incidental text, text on images, logos
- Exception: Black on white or close needs no testing

**Tool:** Color Contrast Analyzer (pacielogroup.com)

---

### PPTX Module 3: Ensuring Color and Other Visual Characteristics that Convey Information are Also Described in Text

**Principle:** Color and visual characteristics (size, shape, location) must have accompanying text descriptions. Without text, blind, low-vision, and colorblind users lack equal access.

**Test:** Identify color/visual characteristic usage > verify text conveys same meaning.

---

### PPTX Module 4: Formatting Columns Correctly

**Key Concept:** "Assistive technology cannot read information in the correct reading order when tabs or spaces have been used to create the look of content being divided into columns."

**Steps to Create:**
1. Select the Home tab
2. In the Paragraph group, locate Add or Remove Columns
3. Choose desired number of columns

**Steps to Test:**
1. Place cursor on columnar text
2. Home tab > Paragraph group > Add or Remove Columns
3. Verify correct number of columns are highlighted

---

### PPTX Module 5: Formatting Lists Properly

**Key Concept:** Proper lists enable assistive technology to identify grouped information and convey relationships.

**Steps:**
1. Home tab > Paragraph group
2. Select Bullets or Numbering option
3. Select feature first then type, or type then apply

**Testing:** Place cursor on list item > check if list formatting indicators are highlighted in Home tab.

**Inaccessible:** Dashes, tabs, or numbers simulating lists without built-in features.
**Accessible:** Built-in bullets or numbering features used.

---

### PPTX Module 6: Using Built-In Features to Create Data Tables

**Requirements:**
1. Use Insert tab > Table group > Insert Table (specify columns/rows)
2. Do NOT merge or split cells
3. Identify headers: Table Design tab > check "header row" and/or "first column"
4. Choose high-contrast table style

**Limitations:** Complex tables (multiple header rows, spanning cells) CANNOT be made accessible in PowerPoint - must convert to remediated PDF.

**Testing:**
1. Select table > verify Table Tools tab (not Picture Format)
2. Tab key through cells > verify no spanning
3. Verify no merged/split cells

---

### PPTX Module 7: Adding Alternative Text to Images and Other Objects

**Guidance:** "Think about the purpose of the image and not what the image looks like."

**Quality Test:** "If you remove the image and replaced it with alternative text and no key information was lost, then chances are you provided the proper amount of descriptions."

**Steps:**
1. Select image/object/shape
2. Right-click or Shift+F10
3. Select "Edit Alt Text"
4. For meaningful images: enter purpose-focused description
5. For decorative objects: enter two spaces between quotes OR select "mark as decorative"
6. Select Close

---

### PPTX Module 8: Creating Links with Unique and Descriptive Names

**Principle:** Each link needs a unique and descriptive name. Multiple "Click Here" links confuse AT users.

**To Insert a Link:**
1. Copy URL > select text > Right-click or Shift+F10 > Link
2. Paste link > OK

**To Change Link Text:**
1. Right-click link > Edit link
2. Text to Display field > enter descriptive text
- Note: "Deleting the last character in the link name will remove the link."

**For Print + Electronic Distribution:** Include both URL and description.

---

### PPTX Module 9: Making Vital Background Information Accessible

**Problem:** "Vital information placed on the slide master, as a watermark, or in header/footer cannot be accessed by assistive technology."

**Solution:** Vital information must be represented in the body of the slide AND headers/footers must be enabled.

**Steps:**
1. Insert tab > Text group > Header and Footer
2. Check Footer option
3. Type vital information
4. Select Apply
5. Verify: Home tab > Drawing > Arrange > Selection Pane
6. Confirm vital information can be selected in Selection Pane

---

### PPTX Module 10: Formatting Text for the Intended Language

**Requirement:** When presentations contain multiple languages, each section must be properly identified. "If the language is not properly associated with the content, assistive technology cannot infer the correct pronunciation."

**Steps:**
1. Select multilingual text
2. Review tab > Language group > Language button
3. "Set Proofing Language"
4. Select appropriate language

---

### PPTX Module 11: Ensuring Descriptions of Embedded Audio, Video and Multimedia Files are Accurate

**Requirements:**
- Audio-only: accurate and complete transcript required
- Video-only: accurate and complete text description required
- Multimedia: accurate and complete synchronized captions AND audio descriptions required

---

### PPTX Module 12: Excluding Flashing Objects

**Rule:** "Flashing objects can cause seizures and should never be used. A Microsoft PowerPoint presentation that includes flashing objects cannot be considered accessible."

---

### PPTX Module 13: Saving in the .pptx Format with a Descriptive Filename

**Requirements:**
1. Save in .pptx format (not .ppt)
2. Use descriptive filename identifying document's purpose

**Inaccessible:** Presentation 1.ppt (non-descriptive, wrong format)
**Accessible:** Accessible electronic documents.pptx (descriptive, correct format)

---

## PART 5: CREATE/TEST GUIDE PAGES

### Create Accessible Documents (/create/documents/)

**Available Guides:**
- Microsoft Word 2016 Basic Authoring and Testing Guide (DOCX)
- Microsoft Word 2016 Printable Accessible Testing (DOCX)
- Microsoft Word 2013 Baseline Test Process, Authoring Guide, Detailed Checklist, Printable Checklist (DOCX)
- Microsoft Word 2010 Authoring Guide, Testing Checklist, Baseline Tests (DOCX)

**Training:** "How to Make an Accessible Document in Microsoft Word" - 14-Part, 59m 21s

**Referenced Standards:**
- Section 508 Standards (access-board.gov/ict/)
- W3C Alt Decision Tree
- WebAIM Alternative Text guidance

### Create Accessible PDFs (/create/pdfs/)

**Policy:** "Federal policy requires agencies to prioritize HTML and use PDFs only when necessary."

**Available Guides:**
- PDF Testing and Remediation Guide 2019 (DOCX)
- PDF Testing Checklist 2019 (DOCX)
- PDF Baseline Test Process 2017 (DOCX)
- PDF Detailed Checklist 2017, Printable Checklist 2017 (DOCX)

**Training:** "How to Test and Remediate PDFs" - 63 minutes, 5 parts

### Create Accessible Spreadsheets (/create/spreadsheets/)

**Available Guides:**
- Microsoft Excel 2016 Basic Authoring and Testing Guide (DOCX)
- Microsoft Excel 2016 Printable Accessible Testing Checklist (DOCX)
- Microsoft Excel 2010 Authoring Guide, Testing Checklist, Baseline Tests (DOCX)

**Training:** "How to Make an Accessible Spreadsheet in Microsoft Excel" - 30m 50s, 12-Part

### Create Accessible Presentations (/create/presentations/)

**Available Guides:**
- Microsoft PowerPoint 2016 Authoring and Testing Guide (DOCX)
- Microsoft PowerPoint 2016 Testing Checklist (DOCX)
- PowerPoint 2010 508-Compliant Guide (PDF) - CMS
- Section 508 Quick Reference Guide - MS PowerPoint 2010 (PDF) - CMS

**Training:** "How to Author and Test Microsoft PowerPoint Presentations for Accessibility" - 43m 54s, 14-Part
**Additional:** "Creating PowerPoint Templates" - 8m 18s

### Test Electronic Documents (/test/documents/)

**Definition:** "A logically distinct assembly of content (such as a file, set of files, or streamed media) that functions as a single entity rather than a collection; is not part of software; and does not include its own software to retrieve and present content."

**Applicability - Public-Facing Documents:** Must conform to Section 508 Standards and WCAG 2.0 Level AA.

**Applicability - Agency Official Communications (Non-Public) must conform when content includes:**
- Emergency notifications
- Administrative claim decisions
- Program/policy announcements
- Benefit/eligibility notices
- Employment opportunities
- Personnel actions
- Formal receipt acknowledgments
- Survey questionnaires
- Templates or forms
- Educational/training materials
- Intranet web page content

**Covered Document Types:** DOCX/Google Docs, PPTX/Google Slides, XLSX/Google Sheets, PDF

**WCAG 2.0 Level A/AA Exceptions for Non-Web Documents (4 criteria excluded):**
1. 2.4.1 Bypass Blocks
2. 2.4.5 Multiple Ways
3. 3.2.3 Consistent Navigation
4. 3.2.4 Consistent Identification

**Word Substitution Rule:** "For non-Web documents, wherever 'Web page' or 'page' appears in WCAG 2.2 Level A and AA Success Criteria, the term 'document' shall be substituted."

---

## PART 6: CROSS-CUTTING REQUIREMENTS SUMMARY

### Common Accessibility Requirements Across All Document Types

| Requirement | Word | Excel | PowerPoint | PDF |
|-------------|------|-------|------------|-----|
| Descriptive filename | Module 1 | Module 10 | Module 13 | Module 2 |
| Correct file format (.docx/.xlsx/.pptx) | Module 1 | Module 10 | Module 13 | N/A |
| Headings/Structure | Module 2 | Module 1 | Module 1 | Module 2-3 |
| Lists | Module 3 | Module 1 | Module 5 | Module 3 |
| Columns/Layout | Module 4 | Module 1 | Module 4 | Module 3 |
| Data Tables | Module 5 | Module 5 | Module 6 | Module 2-3 |
| Language identification | Module 6 | N/A | Module 10 | Module 2-3 |
| Descriptive links | Module 7 | Module 7 | Module 8 | Module 2 |
| Headers/Footers/Watermarks | Module 8 | Module 4 | Module 9 | Module 2 |
| Images/Alt Text | Module 9 | Module 6 | Module 7 | Module 2-3 |
| Textboxes/Floating objects | Module 10 | Module 6 | Module 1 | Module 3 |
| Color + text meaning | Module 11 | Module 3 | Module 3 | Module 2 |
| Color contrast (4.5:1/3:1) | Module 12 | Module 2 | Module 2 | Module 2 |
| Embedded media | Module 13 | Module 8 | Module 11 | N/A |
| No flashing objects | N/A | Module 9 | Module 12 | N/A |

### Tools Referenced Across All Series

| Tool | Purpose |
|------|---------|
| Color Contrast Analyzer (Paciello Group) | Test foreground/background contrast ratios |
| Microsoft Accessibility Checker | Automated check for common issues in Office docs |
| Navigation Pane (Word) | Verify heading structure |
| Reveal Formatting Pane (Shift+F1) | Verify list, column, table, and heading structure |
| Selection Pane (PowerPoint) | Verify and set reading order |
| Adobe Acrobat DC Full Check | Automated PDF accessibility testing |
| Adobe Read Out Loud | Text-to-speech (NOT for conformance testing) |
| Touch-Up Reading Order tool (Acrobat) | Tag and structure PDF content |
| Table Editor (Acrobat) | Set table header scope and span |
| JAWS screen reader | Referenced as proper AT for testing (not built-in tools) |

### WCAG Contrast Ratios (Referenced Across All Types)

- **Standard text:** >= 4.5:1 contrast ratio
- **Large text (14pt bold or 18pt regular):** >= 3:1 contrast ratio
- **Exceptions:** Incidental text, text overlaid on images, logotypes

### WCAG 2.0 Criteria Excluded for Non-Web Documents

1. 2.4.1 Bypass Blocks
2. 2.4.5 Multiple Ways
3. 3.2.3 Consistent Navigation
4. 3.2.4 Consistent Identification
