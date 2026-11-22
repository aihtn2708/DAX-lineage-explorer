@echo off
rem Live lineage without the External Tools button (that one needs administrator rights to install).
rem Open your .pbix in Power BI Desktop first, then run this. It watches the Desktop window opened last.
rem   run-live.bat                          the Desktop window opened last
rem   run-live.bat -Server localhost:51542  a particular one
rem   run-live.bat -ReportPath "C:\Reports\Sales.pbix"   when the page shows no visuals
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dax_lineage\live_tool\PowerBILive.ps1" %*
