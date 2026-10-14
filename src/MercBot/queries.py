UNASSIGNED_PHOTO_ISSUES = """
query UnassignedPhotoIssues($prefix: String!, $after: String) {
  issues(
    first: 100
    after: $after
    filter: {
      title: { startsWith: $prefix }
      assignee: { null: true }
      state: { type: { nin: ["completed", "canceled"] } }
    }
  ) {
    nodes {
      title
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""
