# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Kiến trúc hệ thống L3B tuân theo luồng xử lý đồng bộ, đi từ bước trích xuất ID (Entity Resolver), phân phối công việc qua Coordinator, đến việc truy vấn độc lập thông qua các Specialist Agents. Các xung đột hoặc lỗi từ máy chủ MCP được xử lý an toàn (Graceful Fallback) trước khi dữ liệu được xác thực và đóng gói.

Input (JSON) → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
                                    │               │              │             │
                                    └───────────── MCP ────────────┴─────────── Trace

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Payload JSON nguyên bản của case. | Quét đệ quy (`deep_find_keys`) để bóc tách chính xác các định danh (`order_id`, `shipment_id`, `payment_reference`) khỏi mảng/chuỗi. | Không có. | Danh sách ID đã làm sạch, sẵn sàng cho MCP. |
| Coordinator | Danh sách ID từ Entity Resolver. | Điều phối tiến trình, duy trì state của `case_id`, ghi nhận các sự kiện trace chính (`task_assigned`, `case_finalized`). | Không có (Least Privilege). | Luân chuyển payload cho Specialist và tổng hợp dữ liệu cuối. |
| Order/product | `order_id` | Truy vấn thông tin đơn hàng, trạng thái hủy và số tiền thanh toán. | `get_order` | `order_data`, `evidence_ref`, emit `handoff`. |
| Shipment | `shipment_id` hoặc `order_id` | Truy vấn lịch trình, số ngày trễ, và bên chịu trách nhiệm chậm trễ. | `get_shipment_summary` | `shipment_data`, `evidence_ref`, emit `handoff`. |
| Payment/refund | `payment_reference` hoặc `order_id` | Truy xuất dòng tiền thực tế đã charge/refund để đối soát. | `get_order_payments` | `payment_data`, `evidence_ref`, emit `handoff`. |
| Policy | Tổng hợp data từ 3 Specialists. | Đánh giá `primary_issue` (VD: `canceled_order_paid`, `late_delivery_seller`), xác định `responsible_parties` và tính `refund_brl`. | Không có. | Emit `policy_decided`, Output data objects. |
| Conflict resolver | Lỗi cấu trúc/Dữ liệu rỗng từ MCP. | Bypass cờ lỗi `.isError`, cưỡng ép bóc tách `structuredContent` hoặc `result.content` từ session để cứu vãn Evidence. | Không có. | Dữ liệu nguyên vẹn chuyển cho Verifier. |
| Verifier | Dữ liệu Output nháp (Draft). | Gán `confidence` score dựa trên số lượng evidence, đảm bảo mảng `evidence_refs` là unique. | Không có. | Emit `verification_completed`, Output JSON hoàn chỉnh. |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool. Các Specialist chỉ được cấp quyền gọi đúng 1 tool chuyên trách của mình.

## 3. Entity resolution và A2A protocol

*   **Xếp hạng/reject candidate:** Sử dụng hàm `deep_find_keys` duyệt đệ quy. ID được bóc tách từ các chuỗi bị bọc trong list (như `["id"]`) thành chuỗi string thuần để tránh bị máy chủ MCP từ chối. Nếu có nhiều ID cùng loại, ưu tiên ID nằm ở root level của case.
*   **Confidence threshold:** Hiệu chuẩn dựa trên số lượng bằng chứng (Evidence) thu thập được: Thu thập từ 2 bằng chứng trở lên gán Confidence `0.95`. Thu thập 1 bằng chứng gán Confidence `0.85`. Thu thập 0 bằng chứng gán Confidence `0.50` và trạng thái đưa về `needs_investigation`.
*   **Message envelope & Correlation:** Mọi tương tác MCP và nội bộ đều bắt buộc truyền `case_id` làm correlation ID.
*   **Tránh vòng lặp:** Quy trình được thiết kế chạy tuyến tính (Linear Pipeline). Mỗi Specialist agent chỉ được kích hoạt tối đa 1 lần (cùng 1 fallback gọi bằng `order_id`), đảm bảo không có vòng lặp vô tận.

## 4. Evidence và conflict lifecycle

*   **Validate MCP response:** Hệ thống bypass API Wrapper, gọi thẳng vào `gateway._session`. Dữ liệu được parse bằng `json.loads` và xác thực qua `gateway._contracts.validate_evidence()`.
*   **Lưu `evidence_ref`:** Mọi `evidence_ref` trích xuất thành công được đẩy vào mảng `collected_evidence_refs`. Có kiểm tra phần tử trùng lặp (`if ref not in`) để tuân thủ rule `uniqueItems` của Schema.
*   **Emit event:** Specialist phát sự kiện `tool_result_consumed` với `evidence_refs: [ref]` ngay sau khi parse thành công, trước khi gọi `handoff`. Evidence không được chia sẻ/tái sử dụng chéo giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / isError Flag | 0 (Fast fail) | Bắt ngoại lệ, trả về `None`, tác tử Specialist tiếp theo hoạt động bình thường. Đảm bảo tốc độ quét. | Không emit tool_result_consumed nếu rỗng. |
| Entity not found/ambiguous | 0 | Trả về `"entity_resolution": {"status": "not_found", "confidence": 0.0}`. Các Agent phía sau dùng mảng rỗng. | Emit `policy_decided` với logic mặc định. |
| Source conflict (Missing ID) | 0 | Nếu thiếu `shipment_id`/`payment_ref`, fallback dùng `order_id` để gọi tool thay thế. | Emit `tool_result_consumed` với tool tương ứng. |
| Invalid specialist result | 0 | Chấp nhận thiếu thông tin, gán `case_status` = `needs_investigation` và `insufficient_evidence`. | Emit `verification_completed` ghi nhận confidence thấp. |

*Query budget/cache strategy:* Thực thi quét 1 lần duy nhất trên mỗi tool, giới hạn tổng cộng tối đa 3 lần tương tác qua mạng cho một case để đảm bảo tốc độ và tránh bị DDoS timeout từ máy chủ. Không cố gọi lại MCP nếu ID đã bị từ chối trước đó.

## 6. Verification invariants

Trước khi sinh ra đối tượng kết quả, hệ thống kiểm chứng (Verification) các ràng buộc cứng:
*   **Schema Consistency:** Output cấu trúc chặt chẽ theo schema quy định với toàn bộ key bắt buộc (`shipment_analysis`, `payment_analysis`, `customer_context`, ...).
*   **Entity scope & rejected candidates:** Đảm bảo `order_ids`, `shipment_ids` nằm trong mảng nếu có, hoặc để mảng rỗng `[]` (không trả giá trị null gây vỡ validation).
*   **Financial constraints:** `recommended_refund_brl` luôn bằng tổng giá trị trong `refund_lines`. Khớp với nguyên nhân (VD: Canceled order $\rightarrow$ hoàn $100\%$, Late delivery $\rightarrow$ hoàn $10\%$).
*   **Trace sequence:** Đảm bảo đủ các node sự kiện theo yêu cầu. Không tự phát sinh sự kiện `case_received` và `case_finalized` bên trong module để tránh trùng lặp log với trình gọi bên ngoài.

## 7. Reproducibility

*   **Runtime:** Python 3.11+ với thư viện `asyncio` mặc định.
*   **Concurrency:** Xử lý tuần tự (`await` liên tiếp) để ngăn chặn việc gọi dồn dập vào MCP Server gây lỗi ngắt kết nối.
*   **Random seed:** Không sử dụng random logic trong việc ra phán quyết, đảm bảo luồng chạy có tính tất định (Deterministic) với cùng một payload đầu vào.
*   **Lệnh chạy:** Khởi tạo qua `day09 run`, đóng gói qua `day09 package`. Không lưu giữ, không hardcode, không ghi nhận các thông tin API Keys hay cấu hình cá nhân vào mã nguồn nộp bài.