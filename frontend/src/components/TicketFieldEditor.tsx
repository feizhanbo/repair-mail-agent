import { Button, Form, Input } from 'antd';

export type TicketFieldForm = {
  customer_code?: string;
  customer_name?: string;
  contact_person?: string;
  contact_phone?: string;
  contact_email?: string;
  request_date?: string;
  mailing_address?: string;
  problem_description?: string;
  accessories?: string;
};

type Props = {
  initialValues: TicketFieldForm;
  onSubmit: (values: TicketFieldForm) => Promise<void>;
  loading: boolean;
  onCancel: () => void;
};

export default function TicketFieldEditor({ initialValues, onSubmit, loading, onCancel }: Props) {
  const [form] = Form.useForm<TicketFieldForm>();

  return (
    <Form<TicketFieldForm> form={form} layout="vertical" initialValues={initialValues} onFinish={onSubmit}>
      <Form.Item label="客户代码" name="customer_code"><Input /></Form.Item>
      <Form.Item label="客户名称" name="customer_name"><Input /></Form.Item>
      <Form.Item label="联系人" name="contact_person"><Input /></Form.Item>
      <Form.Item label="联系电话" name="contact_phone"><Input /></Form.Item>
      <Form.Item label="联系邮箱" name="contact_email"><Input /></Form.Item>
      <Form.Item label="报修日期" name="request_date"><Input placeholder="YYYY-MM-DD" /></Form.Item>
      <Form.Item label="寄送地址" name="mailing_address"><Input /></Form.Item>
      <Form.Item label="问题描述" name="problem_description"><Input.TextArea rows={4} /></Form.Item>
      <Form.Item label="附件/配件" name="accessories"><Input /></Form.Item>
      <div style={{ display: 'flex', gap: 8 }}>
        <Button type="primary" htmlType="submit" loading={loading}>保存</Button>
        <Button onClick={onCancel}>取消</Button>
      </div>
    </Form>
  );
}
