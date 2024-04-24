import matplotlib.pyplot as plt
import json

fontsize = 18
markersize = 8
att_target = 90
ylabel = "SLO Attainment (%)"

plt.rcParams.update({'font.size': fontsize})
plt.figure(figsize=(10, 3))

## rate
plt.subplot(1, 2, 1)
xlabel = "Per-GPU Rate (req/s)"
rates = [1, 2, 3, 4, 5]
with open("figure/figure_11a.json") as f:
    data = json.load(f)
    distllm_optimal_SLO_att = data['dist++']
    distllm_real_SLO_att = data['dist']
    vllm_plus_SLO_att = data['vllm++']
    vllm_SLO_att = data['vllm']

plt.plot(rates, distllm_optimal_SLO_att, label='DistLLM-High', marker="o", markersize=markersize)
plt.plot(rates, distllm_real_SLO_att, label='DistLLM-Low', marker="o", markersize=markersize)
plt.plot(rates, vllm_plus_SLO_att, label='vLLM++', marker="o", markersize=markersize)
plt.plot(rates, vllm_SLO_att, label='vLLM', marker="o", markersize=markersize)
plt.plot([rates[0], rates[-1]], [att_target, att_target], '--')
plt.xticks(rates, rates)
plt.xlabel(xlabel)
plt.ylabel(ylabel)

## SLO Scale
plt.subplot(1, 2, 2)
xlabel = "SLO Scale"
SLO_scales = [1, 2, 3, 4, 5]
with open("figure/figure_11a.json") as f:
    data = json.load(f)
    distllm_optimal_SLO_att = data['dist++']
    distllm_real_SLO_att = data['dist']
    vllm_plus_SLO_att = data['vllm++']
    vllm_SLO_att = data['vllm']

plt.plot(rates, distllm_optimal_SLO_att, label='DistLLM-High', marker="o", markersize=markersize)
plt.plot(rates, distllm_real_SLO_att, label='DistLLM-Low', marker="o", markersize=markersize)
plt.plot(rates, vllm_plus_SLO_att, label='vLLM++', marker="o", markersize=markersize)
plt.plot(rates, vllm_SLO_att, label='vLLM', marker="o", markersize=markersize)
plt.plot([rates[0], rates[-1]], [att_target, att_target], '--')
plt.xticks(rates, rates)
plt.xlabel(xlabel)

# plt.legend(frameon=False, bbox_to_anchor = (0.75, 1.3, 0, 0), ncol=2,
#            bbox_transform = plt.gcf().transFigure, columnspacing=1)

plt.legend(frameon=False, bbox_to_anchor=(0.95, 1.1, 0, 0), ncol=4,
           bbox_transform=plt.gcf().transFigure, columnspacing=1)
plt.savefig("figure/ablation.pdf", bbox_inches="tight")