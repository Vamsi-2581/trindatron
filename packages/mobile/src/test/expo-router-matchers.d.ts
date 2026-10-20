// expo-router/testing-library registers these matchers at runtime, but SDK 57 ships no type declaration for them.
declare global {
  namespace jest {
    // Declaration merging requires the exact type parameters of @types/jest's Matchers<R, T = {}>.
    // eslint-disable-next-line @typescript-eslint/no-unused-vars, @typescript-eslint/no-empty-object-type
    interface Matchers<R, T = {}> {
      toHavePathname(pathname: string): R
      toHaveSegments(segments: string[]): R
    }
  }
}

export {}
