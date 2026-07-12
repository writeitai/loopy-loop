// Thin client-side wrapper around Pagefind.
//
// Pagefind indexes the static export at build time (see `postbuild` in
// package.json) and writes a small JS API to `/pagefind/pagefind.js`. That file
// only exists in the deployed `out/` build — not during `next dev` — so we load
// it lazily and indirectly, keeping the bundler from trying to resolve it.

export type PagefindResult = {
  url: string;
  title: string;
  excerpt: string;
};

type PagefindDocument = {
  url: string;
  meta?: { title?: string };
  excerpt: string;
};

type PagefindApi = {
  init?: () => Promise<void>;
  search: (query: string) => Promise<{
    results: Array<{ id: string; data: () => Promise<PagefindDocument> }>;
  }>;
};

// `new Function` keeps webpack/Turbopack from statically analysing the import.
const dynamicImport = new Function(
  "path",
  "return import(path)"
) as (path: string) => Promise<PagefindApi>;

let pagefindPromise: Promise<PagefindApi | null> | null = null;

function importPagefind(): Promise<PagefindApi | null> {
  return dynamicImport("/pagefind/pagefind.js")
    .then(async (mod) => {
      if (mod.init) await mod.init();
      return mod;
    })
    .catch(() => null);
}

/** Load (once) the Pagefind API, or null if the index is unavailable. */
export function loadPagefind(): Promise<PagefindApi | null> {
  if (!pagefindPromise) {
    pagefindPromise = importPagefind();
  }
  return pagefindPromise;
}

/** Run a search and return up to 8 shaped results. Empty query -> []. */
export async function searchDocs(query: string): Promise<PagefindResult[]> {
  const pagefind = await loadPagefind();
  if (!pagefind || !query.trim()) return [];
  const search = await pagefind.search(query);
  const top = search.results.slice(0, 8);
  const docs = await Promise.all(top.map((r) => r.data()));
  return docs.map((d) => ({
    url: d.url,
    title: d.meta?.title ?? d.url,
    excerpt: d.excerpt,
  }));
}
