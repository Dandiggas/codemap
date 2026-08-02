export function approve(id: number): Promise<Response> {
  return fetch(`/api/orders/${id}`);
}
