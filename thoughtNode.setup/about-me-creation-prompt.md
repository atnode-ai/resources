Reusable prompt for building (or rebuilding) about-me.md, the global identity file Claude reads in every project.

## How to use it

Paste the block below into a Claude session. Claude asks the questions first, then writes about-me.md.

## The prompt

```
I want to build my about-me.md from scratch. This file loads in every project, so keep it identity-level, not tied to one company or job.

Ask me the following, one question at a time, before writing anything:

1. Who am I? Current role(s), any companies I'm affiliated with, where I'm based, languages I work in.
2. Professional background: career path so far, key roles held, what I actually specialize in.
3. Is there a trajectory or plan worth stating (e.g. "ramping X up over N years, timed with leaving Y")? If none, skip it.
4. Interests outside the job description: intellectual/professional curiosities, and anything non-work that matters (health, hobbies, whatever keeps me sane).
5. Anything that belongs in a separate, company-scoped file instead of here (so it doesn't get loaded globally by mistake)?

Once you have my answers, write about-me.md:
- "# ABOUT ME: [name]" as the title.
- "## Who I am": current roles, affiliations, location, languages. Short.
- "## Professional background": career history and specialties, in prose, not bullets.
- "## Interests": professional and personal, a paragraph each at most.

No filler, no bullet lists unless something genuinely needs one. Prose, short paragraphs, matching my anti-ai-writing-style.md rules. Save it into the ABOUT ME folder.
```

## Notes

- Matches the structure of the existing about-me.md (see that file for a worked example).
- Company-specific facts (goals, focus, editorial standards, bets) belong in a scoped my-company.md, not here — this prompt asks about that split in question 5.
- Re-running this prompt later, to refresh rather than rebuild, works fine: just tell Claude to update the existing file instead of starting blank.