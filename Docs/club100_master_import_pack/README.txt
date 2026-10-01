Club100 Master Import Pack

Source of truth:
- Uploaded workbook: Club100 - Health & Wellness Assessment (Responses) (3).xlsx
- Canonical categories and weights are taken from the Weights sheet.
- Scoring benchmark rows are taken from Config.

IMPORTANT
1. Files 01, 02, 04 and 07 are parent/master data CSVs.
2. Files 03, 05 and 06 represent child-table/mapping data in normalized form. Frappe Data Import child-table templates may use different column headers; use these as the source rows or load them through a seed/patch script.
3. File 08 preserves the exact XLS scoring benchmark model. DO NOT import it into the CURRENT Club100 Metric Scoring Rule schema yet. The current rule schema (min/max/score/rating) cannot faithfully represent Excel methods such as Linear-Higher, Linear-Lower and Range with Excellent/Risk/Poor benchmarks. Update the scoring-rule schema first.
4. Required flags are not defined in the XLS. File 05 uses proposed defaults and File 10 calls them out explicitly. Review before production.
5. 'Well Being' from the Weights sheet is normalized to 'Well-being & Readiness'.

Recommended import order after cleanup:
01 Assessment Inputs
02 Fitness Metrics
03 Metric→Input mappings
04 Assessment Template
05 Template Items
06 Category Weights
07 Scoring Config parents
08 Scoring benchmarks only AFTER scoring schema is updated

Do not delete DocType definitions. Clean only test/master records listed in the accompanying cleanup guidance.
