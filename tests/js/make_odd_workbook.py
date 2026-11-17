"""A workbook in the shapes openpyxl never writes: inline strings, rich-text
shared strings, a title row above the header, cells without r= references,
an absolute relationship target and stored (uncompressed) entries."""
import sys, zipfile

parts = {
 "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
 "xl/workbook.xml": '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Result" sheetId="1" r:id="rId7"/></sheets></workbook>',
 "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId7" Type="worksheet" Target="/xl/worksheets/data.xml"/></Relationships>',
 "xl/sharedStrings.xml": '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Name</t></si><si><r><t>Expr</t></r><r><rPr><b/></rPr><t>ession</t></r></si><si><t>Total Sales</t></si></sst>',
 "xl/worksheets/data.xml": '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
   '<row r="1"><c r="A1" t="inlineStr"><is><t>DAX query result</t></is></c></row>'
   '<row r="3"><c t="s"><v>0</v></c><c t="s"><v>1</v></c><c r="D3" t="inlineStr"><is><t>IsHidden</t></is></c></row>'
   '<row r="4"><c r="A4" t="s"><v>2</v></c><c r="B4" t="inlineStr"><is><t>SUM ( Sales[Amount] )</t></is></c><c r="D4" t="b"><v>1</v></c></row>'
   '<row r="5"><c r="A5" t="str"><v>Margin</v></c><c r="B5" t="inlineStr"><is><r><t>[Total </t></r><r><t>Sales] * 0.3</t></r></is></c><c r="D5" t="b"><v>0</v></c></row>'
   '</sheetData></worksheet>',
}
with zipfile.ZipFile(sys.argv[1], "w") as z:
    for i, (name, body) in enumerate(parts.items()):
        z.writestr(name, body, compress_type=zipfile.ZIP_STORED if i % 2 else zipfile.ZIP_DEFLATED)
