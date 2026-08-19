import { defineEnterpriseEval, enterpriseTasks } from "./niceeval/enterprise.js";

export default Object.fromEntries(
  enterpriseTasks.map((task) => [task.id, defineEnterpriseEval(task)]),
);
