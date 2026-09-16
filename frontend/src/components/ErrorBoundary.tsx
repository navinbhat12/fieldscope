import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** Rendered in place of the subtree when it throws. */
  fallback: (message: string) => ReactNode
}

interface State {
  message: string | null
}

/**
 * Keeps one failing subtree from taking the page with it.
 *
 * WebGL map initialisation can fail for reasons that have nothing to do with
 * this application -- no GPU, a blocked worker, a browser with WebGL disabled.
 * React's default for an uncaught render error is to unmount the whole tree,
 * which turns any of those into a blank white page that says nothing. A demo
 * that cannot draw its map should still show its readout and say why.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { message: null }

  static getDerivedStateFromError(error: unknown): State {
    return { message: error instanceof Error ? error.message : String(error) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Fieldscope: subtree failed', error, info.componentStack)
  }

  render() {
    return this.state.message === null
      ? this.props.children
      : this.props.fallback(this.state.message)
  }
}
