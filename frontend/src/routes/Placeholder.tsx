import { EmptyState } from '@/components/states'
import { PageHeader } from '@/components/PageHeader'

/** Stands in for screens that arrive in a later phase, so navigation is walkable now. */
export function Placeholder({ title, phase }: { title: string; phase: string }) {
  return (
    <>
      <PageHeader title={title} />
      <EmptyState
        title={`${title} is not built yet`}
        description={`Scheduled for ${phase}. The route, guard, and navigation entry are already wired, so only the screen itself is missing.`}
      />
    </>
  )
}
