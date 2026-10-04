// Content collections. This Markdown is the single source of truth for BOTH the
// website pages AND the assistant's knowledge base (scripts/sync_content.py turns
// it into the RAG corpus, keeping each page's URL so citations can link to it).
import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

const practice = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/practice' }),
  schema: z.object({
    title: z.string(),
    summary: z.string(),
    order: z.number(),
  }),
});

const people = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/people' }),
  schema: z.object({
    name: z.string(),
    role: z.string(),
    practices: z.array(z.string()),
    order: z.number(),
  }),
});

const insights = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/insights' }),
  schema: z.object({
    title: z.string(),
    summary: z.string(),
    date: z.coerce.date(),
    practice: z.string(),
    author: z.string(),
    sources: z.array(z.string()),
  }),
});

const pages = defineCollection({
  loader: glob({ pattern: '*.md', base: './src/content/pages' }),
  schema: z.object({
    title: z.string(),
    summary: z.string(),
  }),
});

export const collections = { practice, people, insights, pages };
