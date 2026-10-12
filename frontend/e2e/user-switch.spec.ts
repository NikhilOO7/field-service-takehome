// Smoke test (spec §12): switching users is a new session; nothing from the previous user stays visible,
// and the server only returns the new user's conversations. Needs the API (:8000) and web (:5173) running.
import { expect, test } from '@playwright/test'

test('switching user clears the previous user state', async ({ page }) => {
  // No LLM call is needed to check isolation: sign in, list Daniel's conversations, switch, compare.
  await page.goto('/')
  await page.getByRole('button', { name: /Daniel Reyes/ }).click()
  await expect(page.getByText('Hi Daniel, what are you working on?')).toBeVisible()
  const danielConvs = await page.evaluate(async () => (await (await fetch('/api/conversations')).json()).conversations)

  await page.getByRole('button', { name: 'Account menu' }).click()
  await page.getByRole('button', { name: /Priya Natarajan/ }).click()
  await expect(page.getByRole('button', { name: 'Account menu' })).toContainText('Priya Natarajan')
  const priyaConvs = await page.evaluate(async () => (await (await fetch('/api/conversations')).json()).conversations)

  const priyaIds = new Set(priyaConvs.map((c: { id: string }) => c.id))
  for (const c of danielConvs) expect(priyaIds.has(c.id)).toBe(false)
  for (const c of danielConvs) {
    const status = await page.evaluate(async (id) => (await fetch(`/api/conversations/${id}`)).status, c.id)
    expect(status).toBe(404)
  }
  // None of Daniel's questions may still be on screen.
  type Conv = { id: string; first_question: string | null }
  for (const c of danielConvs as Conv[]) {
    const q = c.first_question
    if (q && !(priyaConvs as Conv[]).some((p) => p.first_question === q)) {
      await expect(page.getByText(q, { exact: true })).toHaveCount(0)
    }
  }
})
