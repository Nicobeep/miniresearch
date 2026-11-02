# Workflow: PubMed Reading List Builder + Anthropic/Gemini

A single web page that helps you build a PubMed search, pick articles, and export them. It has no server and no install step.

## Setup (once)

Live at: http://127.0.0.1:8080/

## Steps

1. **Describe the topic (optional).** In the **Query helper**, type your question in plain language and click **Suggest query**.
   - Reply in the same chat to refine the query.
   - Click **Use in search box** to copy a suggestion into the search form.
   - Each suggestion is checked against PubMed automatically.
2. **Check the query.** Click **Check query** (no AI or key needed). The page reports:
   - how many results PubMed returns,
   - whether each MeSH term exists (a red term was not found; confirm the exact heading in the MeSH Browser),
   - any warnings from PubMed, plus how PubMed interpreted the query.
3. **Set filters.** Optionally set a from-year and to-year, an article type (systematic review, meta-analysis, review, randomized or clinical trial, case report), a sort order, and the number of results (20, 50 or 100).
4. **Search.** Click **Search PubMed**. Each result shows the title, authors, journal, year, article-type tags, an expandable abstract, the PMID and a DOI link.
5. **Select.** Every result starts selected. Untick the ones you don't want, or use **Select all** and **Clear**.
6. **Export.**
   - **Export RIS** for EndNote, Zotero or Mendeley.
   - **Export CSV** for Excel or Google Sheets.
   - **Copy citations** for a numbered list you can paste.
7. **Verify.** Open each PubMed or DOI link and check the citation before using it.

## Rules

- Do not enter patient data or protected health information anywhere on the page.
- Search terms go to NCBI PubMed. Anything typed in the query helper also goes to the AI provider you chose. Check your institution's policy before using unpublished study details.
- AI-suggested queries are starting points. Check them yourself, and have a health sciences librarian review any search you report.
- Free AI tiers have daily limits per model. If you hit one, wait or change the model name in AI settings.

## Checklist before using any results

- [ ] Each citation opened and checked against PubMed or the DOI
- [ ] No patient data or restricted details typed into the page
- [ ] Query reviewed, and MeSH warnings resolved
- [ ] Institution's policy on AI tools and data confirmed
