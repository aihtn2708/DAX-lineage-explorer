# Third-party notices

## XPress9 (Microsoft)

`docs/js/xpress9.js` and `dax_lineage/pbix_model.py` contain a JavaScript and
a Python port of the decoder in Microsoft's XPress9 compression library,
which Microsoft publishes under the MIT License in
<https://github.com/microsoft/Extensible-Storage-Engine>
(`dev/ese/src/_xpress9/`). Only the decoding side is ported. The ports were
written from the C source by reading it; no Microsoft code is shipped as is.

The port was checked against a real Power BI file: its decompressed model
yields a SQLite database that passes `PRAGMA integrity_check`, and every
value in its tables matches what Python's `sqlite3` reads.

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
