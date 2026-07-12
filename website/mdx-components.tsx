import type { MDXComponents } from "mdx/types";

// Required by @next/mdx. Global MDX element styling is handled by the
// `prose` classes on the docs <article>; add component overrides here if needed.
export function useMDXComponents(components: MDXComponents): MDXComponents {
  return { ...components };
}
